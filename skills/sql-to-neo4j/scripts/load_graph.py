#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg[binary]>=3.2",
#     "neo4j>=5.28,<6",
# ]
# ///
"""
Load a PostgreSQL table into Neo4j according to a model.json, create the
supporting constraints/indexes, run a first visualization query, and print the
Neo4j Browser URL (pre-seeded with that query) for the user.

Design choices worth knowing:
  * Everything is idempotent. Nodes are MERGE-d on their key, so re-running the
    loader updates rather than duplicates. You can iterate on the model and
    reload without wiping the database.
  * We create a uniqueness CONSTRAINT on every node key first. In Neo4j a unique
    constraint is also backed by an index, so this both guarantees identity and
    makes the MERGE fast -- without it, MERGE on a big table does a full scan per
    row and crawls.
  * Rows stream in batches inside a single UNWIND per batch, which is dramatically
    faster than one query per row.

Usage:
  load_graph.py --model model.json --dsn <pg-dsn> --env-file .neo4j.env \
                [--batch-size 1000] [--wipe-label]
"""

import argparse
import hashlib
import json
import sys
import urllib.parse

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.exit("psycopg is required — run this script via `uv run` so its inline dependencies are provisioned")

try:
    from neo4j import GraphDatabase
except ImportError:
    sys.exit("neo4j driver is required — run this script via `uv run` so its inline dependencies are provisioned")


def load_env_file(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    return env


def split_table(table):
    return table.split(".", 1) if "." in table else ("public", table)


def synth_key(row, cols):
    h = hashlib.sha1()
    for c in cols:
        h.update(repr(row.get(c)).encode("utf-8"))
    return h.hexdigest()


def make_constraints(session, model):
    """Unique constraint (and thus index) on every node key we MERGE on."""
    stmts = []
    pn = model["primary_node"]
    for k in pn["key_props"]:
        stmts.append(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{pn['label']}`) REQUIRE n.`{k}` IS UNIQUE")
    seen = set()
    for r in model["relationships"]:
        key = (r["to_label"], r["to_key_prop"])
        if key in seen:
            continue
        seen.add(key)
        stmts.append(
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{r['to_label']}`) REQUIRE n.`{r['to_key_prop']}` IS UNIQUE"
        )
    for s in stmts:
        session.run(s)
    return stmts


def build_row_cypher(model):
    """One parameterized query that MERGEs the primary node + all its edges."""
    pn = model["primary_node"]
    label = pn["label"]
    key_props = pn["key_props"]

    key_match = ", ".join(f"`{k}`: row.`{k}`" for k in key_props)
    lines = ["UNWIND $rows AS row", f"MERGE (n:`{label}` {{{key_match}}})", "SET n += row.props"]

    for i, r in enumerate(model["relationships"]):
        var = f"m{i}"
        col = r["via_column"]
        # Neo4j 5 variable-scope subquery: CALL (n, row) { ... } imports n and row.
        lines.append(
            f"WITH n, row "
            f"CALL (n, row) {{ "
            f"WITH n, row WHERE row.`{col}` IS NOT NULL "
            f"MERGE ({var}:`{r['to_label']}` {{`{r['to_key_prop']}`: row.`{col}`}}) "
            f"MERGE (n)-[:`{r['rel_type']}`]->({var}) }}"
        )
    return "\n".join(lines)


def stream_rows(cur, schema, name, columns):
    colnames = [c["name"] for c in columns]
    select_cols = sql.SQL(", ").join(sql.Identifier(c) for c in colnames)
    cur.execute(sql.SQL("SELECT {} FROM {}.{}").format(select_cols, sql.Identifier(schema), sql.Identifier(name)))
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            break
        for rec in batch:
            yield dict(zip(colnames, rec, strict=True))


def to_neo4j_value(v):
    # Neo4j has no native date/decimal python passthrough for some types; stringify safely
    import datetime
    import decimal

    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


def load_join(model, pg_dsn, env, batch_size):
    """Join-table mode: every row becomes a relationship with properties."""
    j = model["join_relationship"]
    schema, name = split_table(model["source"]["table"])
    prop_cols = j["property_columns"]

    driver = GraphDatabase.driver(
        env["NEO4J_URI"],
        auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]),
        warn_notification_severity="OFF",
    )
    pg = psycopg.connect(pg_dsn)
    total = skipped = 0
    row_cypher = (
        "UNWIND $rows AS row\n"
        f"MERGE (a:`{j['from_label']}` {{`{j['from_key_prop']}`: row.`{j['from_via']}`}})\n"
        f"MERGE (b:`{j['to_label']}` {{`{j['to_key_prop']}`: row.`{j['to_via']}`}})\n"
        f"MERGE (a)-[r:`{j['rel_type']}`]->(b)\n"
        "SET r += row.props"
    )
    try:
        with driver.session() as session:
            print("Creating constraints + indexes...")
            for label, key in ((j["from_label"], j["from_key_prop"]), (j["to_label"], j["to_key_prop"])):
                stmt = f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.`{key}` IS UNIQUE"
                session.run(stmt)
                print("  " + stmt)
            with pg.cursor(name="pg_stream") as cur:
                cur.itersize = 5000
                buf = []
                for raw in stream_rows(cur, schema, name, model["columns"]):
                    row = {k: to_neo4j_value(v) for k, v in raw.items()}
                    if row.get(j["from_via"]) is None or row.get(j["to_via"]) is None:
                        skipped += 1  # a NULL endpoint can't become a relationship
                        continue
                    buf.append(
                        {
                            j["from_via"]: row[j["from_via"]],
                            j["to_via"]: row[j["to_via"]],
                            "props": {c: row.get(c) for c in prop_cols},
                        }
                    )
                    if len(buf) >= batch_size:
                        session.run(row_cypher, rows=buf)
                        total += len(buf)
                        print(f"  loaded {total} rows...", end="\r")
                        buf = []
                if buf:
                    session.run(row_cypher, rows=buf)
                    total += len(buf)
            print(f"  loaded {total} rows as relationships." + (f" ({skipped} skipped: NULL endpoint)" if skipped else ""))
            counts = {}
            for lbl in (j["from_label"], j["to_label"]):
                counts[lbl] = session.run(f"MATCH (n:`{lbl}`) RETURN count(n) AS c").single()["c"]
            rels = session.run(f"MATCH ()-[r:`{j['rel_type']}`]->() RETURN count(r) AS c").single()["c"]
    finally:
        pg.close()
        driver.close()
    return total, counts, rels


def load(model, pg_dsn, env, batch_size, wipe_label):
    pn = model["primary_node"]
    label = pn["label"]
    key_props = pn["key_props"]
    synth_from = pn.get("synthetic_key_from")
    prop_cols = pn["property_columns"]
    rel_cols = [r["via_column"] for r in model["relationships"]]
    schema, name = split_table(model["source"]["table"])

    driver = GraphDatabase.driver(
        env["NEO4J_URI"],
        auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]),
        warn_notification_severity="OFF",  # keep the deprecation chatter quiet
    )
    pg = psycopg.connect(pg_dsn)
    total = 0
    try:
        with driver.session() as session:
            if wipe_label:
                print(f"Wiping existing (:{label}) nodes...")
                session.run(f"MATCH (n:`{label}`) DETACH DELETE n")
            print("Creating constraints + indexes...")
            for s in make_constraints(session, model):
                print("  " + s)

            row_cypher = build_row_cypher(model)
            with pg.cursor(name="pg_stream") as cur:  # server-side cursor
                cur.itersize = 5000
                buf = []
                for raw in stream_rows(cur, schema, name, model["columns"]):
                    row = {k: to_neo4j_value(v) for k, v in raw.items()}
                    # node key
                    keyed = {}
                    if synth_from:
                        keyed["_row_key"] = synth_key(row, synth_from)
                    else:
                        for k in key_props:
                            keyed[k] = row.get(k)
                    props = {c: row.get(c) for c in prop_cols}
                    rec = {**keyed, "props": props}
                    for rc in rel_cols:
                        rec[rc] = row.get(rc)
                    buf.append(rec)
                    if len(buf) >= batch_size:
                        session.run(row_cypher, rows=buf)
                        total += len(buf)
                        print(f"  loaded {total} rows...", end="\r")
                        buf = []
                if buf:
                    session.run(row_cypher, rows=buf)
                    total += len(buf)
            print(f"  loaded {total} rows.      ")

            # summary counts
            counts = {}
            for lbl in {label} | {r["to_label"] for r in model["relationships"]}:
                n = session.run(f"MATCH (n:`{lbl}`) RETURN count(n) AS c").single()["c"]
                counts[lbl] = n
            rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    finally:
        pg.close()
        driver.close()
    return total, counts, rels


def viz_query(model):
    """A first query that returns a connected, colorful subgraph to visualize."""
    if model.get("mode") == "join_table":
        j = model["join_relationship"]
        return f"MATCH p=(a:`{j['from_label']}`)-[r:`{j['rel_type']}`]->(b:`{j['to_label']}`)\nRETURN p LIMIT 300"
    pn = model["primary_node"]
    if model["relationships"]:
        return f"MATCH p=(n:`{pn['label']}`)-[r]->(m)\nRETURN p LIMIT 300"
    return f"MATCH (n:`{pn['label']}`)\nRETURN n LIMIT 300"


def browser_url(env, query):
    # Neo4j Browser accepts a pre-seeded command via cmd/arg, and dbms/db
    # pre-fill the connection form so the user only types the password.
    base = env["NEO4J_HTTP"]
    encoded = urllib.parse.quote(query)
    bolt_host = env["NEO4J_URI"].split("://", 1)[-1]  # host:port
    dbms = urllib.parse.quote(f"neo4j://{env['NEO4J_USER']}@{bolt_host}", safe="")
    return f"{base}/browser/?dbms={dbms}&db=neo4j&cmd=edit&arg={encoded}"


def starter_queries(model):
    """Model-shaped 'first queries' with escalating wow, each with a SQL hint.

    Returns a list of {title, cypher, sql_hint} dicts. Purely derived from the
    model: dimensions give familiar GROUP BY-style counts, any relationship
    gives co-occurrence through a shared neighbor, and two relationships give
    a shortest-path party trick.
    """
    if model.get("mode") == "join_table":
        j = model["join_relationship"]
        fl, tl, rt = j["from_label"], j["to_label"], j["rel_type"]
        out = [
            {
                "title": f"Busiest {tl}: which get linked the most",
                "cypher": (
                    f"MATCH (:`{fl}`)-[r:`{rt}`]->(t:`{tl}`)\n"
                    f"RETURN t.`{j['to_key_prop']}` AS `{tl.lower()}`, count(r) AS links ORDER BY links DESC LIMIT 10"
                ),
                "sql_hint": f"SELECT {j['to_via']}, COUNT(*) FROM ... GROUP BY 1 ORDER BY 2 DESC",
            },
            {
                "title": f"Bought-together: {tl} pairs sharing a {fl}",
                "cypher": (
                    f"MATCH (p1:`{tl}`)<-[:`{rt}`]-(:`{fl}`)-[:`{rt}`]->(p2:`{tl}`)\n"
                    f"WHERE elementId(p1) < elementId(p2)\n"
                    f"RETURN p1.`{j['to_key_prop']}` AS a, p2.`{j['to_key_prop']}` AS b, count(*) AS together "
                    f"ORDER BY together DESC LIMIT 10"
                ),
                "sql_hint": "the classic co-occurrence self-JOIN — the query recommendation engines start from",
            },
        ]
        numeric_types = {
            "smallint", "integer", "bigint", "numeric", "decimal",
            "real", "double precision", "money",
        }
        col_types = {c["name"]: c["type"].lower() for c in model.get("columns", [])}
        numeric_props = [p for p in j["property_columns"] if col_types.get(p) in numeric_types]
        if numeric_props:
            p = numeric_props[0]
            out.append(
                {
                    "title": f"Relationship properties in action: total `{p}` per {fl}",
                    "cypher": (
                        f"MATCH (f:`{fl}`)-[r:`{rt}`]->()\n"
                        f"RETURN f.`{j['from_key_prop']}` AS `{fl.lower()}`, sum(r.`{p}`) AS `total_{p}` "
                        f"ORDER BY `total_{p}` DESC LIMIT 10"
                    ),
                    "sql_hint": f"SELECT {j['from_via']}, SUM({p}) FROM ... GROUP BY 1 — but here {p} lives ON the arrow",
                }
            )
        return out
    pn = model["primary_node"]
    label = pn["label"]
    dims = [r for r in model["relationships"] if r["kind"] == "dimension"]
    fks = [r for r in model["relationships"] if r["kind"] == "foreign_key"]
    out = []

    if dims:
        d = dims[0]
        out.append(
            {
                "title": f"Count {label} rows per {d['to_label']} (familiar ground)",
                "cypher": (
                    f"MATCH (n:`{label}`)-[:`{d['rel_type']}`]->(d:`{d['to_label']}`)\n"
                    f"RETURN d.value AS `{d['via_column']}`, count(n) AS rows ORDER BY rows DESC"
                ),
                "sql_hint": f"SELECT {d['via_column']}, COUNT(*) FROM ... GROUP BY {d['via_column']}",
            }
        )
    anchor = fks[0] if fks else (dims[0] if dims else None)
    if anchor:
        out.append(
            {
                "title": f"Co-occurrence: {label} pairs sharing the same {anchor['to_label']}",
                "cypher": (
                    f"MATCH (a:`{label}`)-[:`{anchor['rel_type']}`]->(x)<-[:`{anchor['rel_type']}`]-(b:`{label}`)\n"
                    f"WHERE elementId(a) < elementId(b)\n"
                    f"RETURN x, count(*) AS pairs ORDER BY pairs DESC LIMIT 10"
                ),
                "sql_hint": "a self-JOIN through the shared column (two aliases of the same table)",
            }
        )
    if fks:
        f = fks[0]
        out.append(
            {
                "title": f"Busiest {f['to_label']}: most connected hubs",
                "cypher": (
                    f"MATCH (t:`{f['to_label']}`)<-[:`{f['rel_type']}`]-(n:`{label}`)\n"
                    f"RETURN t.`{f['to_key_prop']}` AS `{f['to_label'].lower()}`, count(n) AS degree "
                    f"ORDER BY degree DESC LIMIT 10"
                ),
                "sql_hint": f"SELECT {f['via_column']}, COUNT(*) FROM ... GROUP BY 1 ORDER BY 2 DESC",
            }
        )
    if len(model["relationships"]) >= 2:
        out.append(
            {
                "title": f"Party trick: shortest path between two {label} rows (any route)",
                "cypher": (
                    f"MATCH (a:`{label}`), (b:`{label}`) WHERE elementId(a) < elementId(b)\n"
                    f"WITH a, b LIMIT 1\n"
                    f"MATCH p = shortestPath((a)-[*..6]-(b)) RETURN p"
                ),
                "sql_hint": "recursive CTE territory in SQL — one function call here",
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="model.json")
    ap.add_argument("--dsn", required=True, help="postgres DSN")
    ap.add_argument("--env-file", default=".neo4j.env")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument(
        "--wipe-label", action="store_true", help="delete existing nodes of the primary label before loading"
    )
    args = ap.parse_args()

    with open(args.model) as f:
        model = json.load(f)
    env = load_env_file(args.env_file)

    if model.get("mode") == "join_table":
        if args.wipe_label:
            print("Note: --wipe-label applies to node-table mode only; ignored for a join-table model "
                  "(relationships are MERGEd on their endpoints, so re-runs update in place).")
        total, counts, rels = load_join(model, args.dsn, env, args.batch_size)
    else:
        total, counts, rels = load(model, args.dsn, env, args.batch_size, args.wipe_label)

    q = viz_query(model)
    url = browser_url(env, q)

    print("\nTransfer complete.")
    print("  Nodes by label:")
    for lbl, n in counts.items():
        print(f"    (:{lbl}) -> {n}")
    print(f"  Relationships  -> {rels}")
    print("\nVisualization query:")
    print("  " + q.replace("\n", "\n  "))
    print("\nOpen Neo4j Browser here (connection pre-filled, query pre-loaded):")
    print("  " + url)
    print(f"\n(Log in with user '{env['NEO4J_USER']}' if prompted, then press the ▶ run button.)")

    starters = starter_queries(model)
    if starters:
        print("\nStarter queries to try next (each with its SQL equivalent):")
        for i, s in enumerate(starters, 1):
            print(f"\n  {i}. {s['title']}")
            print("     " + s["cypher"].replace("\n", "\n     "))
            print(f"     -- in SQL terms: {s['sql_hint']}")

    # emit machine-readable summary for the skill to relay
    print("\n===RESULT_JSON===")
    print(
        json.dumps(
            {
                "rows_loaded": total,
                "node_counts": counts,
                "relationships": rels,
                "browser_url": url,
                "viz_query": q,
                "starter_queries": starters,
                "bolt": env["NEO4J_URI"],
                "user": env["NEO4J_USER"],
            }
        )
    )


if __name__ == "__main__":
    main()

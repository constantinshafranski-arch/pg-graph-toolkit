#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg[binary]>=3.2",
#     "neo4j>=5.28,<6",
# ]
# ///
"""Keep the graph in sync with its source table — and prove it.

Subcommands:
  verify  Compare Postgres vs graph: row/relationship counts plus a random
          PK spot-check (N random source keys must exist in the graph).
          Read-only on both sides. Exit 0 when consistent, 3 when not.
  run     Re-sync. Incremental when --updated-column is given and a high-water
          mark exists (WHERE col >= last mark — ties re-MERGE harmlessly);
          full re-stream otherwise. Loading is idempotent MERGE either way.
          State lives in .neo4j-sync-state.json in the project directory.

Deliberate limitations (documented, not hidden): deletes in Postgres are NOT
propagated (the graph is a disposable derived view — do a full reload with
--wipe-label via load_graph.py for that), and incremental mode only catches
rows whose --updated-column moved (inserts for a created_at column; updates
too only if the app maintains an updated_at). Rows committed concurrently
DURING a sync with a column value below the recorded max can be missed —
when in doubt, re-run without --updated-column state (full re-merge) or
check with `verify`.

Run with:  uv run sync_graph.py verify --model model.json --dsn "$DSN"
           uv run sync_graph.py run --model model.json --dsn "$DSN" [--updated-column updated_at]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load_graph as lg  # shared: env parsing, cypher building, value coercion  # noqa: E402

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.exit("psycopg is required — run this script via `uv run` so its inline dependencies are provisioned")
from neo4j import GraphDatabase  # noqa: E402

STATE_FILE = ".neo4j-sync-state.json"


def read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def pg_connect(dsn):
    return psycopg.connect(dsn)


def cmd_verify(args, model, env):
    schema, name = lg.split_table(model["source"]["table"])
    join = model.get("mode") == "join_table"
    out: dict = {"table": model["source"]["table"], "mode": model.get("mode", "node_table")}

    with pg_connect(args.dsn) as pg, pg.cursor() as cur, GraphDatabase.driver(
        env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]), warn_notification_severity="OFF"
    ) as driver, driver.session() as session:
        if join:
            j = model["join_relationship"]
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {} IS NOT NULL AND {} IS NOT NULL").format(
                    sql.Identifier(schema), sql.Identifier(name),
                    sql.Identifier(j["from_via"]), sql.Identifier(j["to_via"]),
                )
            )
            pg_count = cur.fetchone()[0]
            graph_count = session.run(
                f"MATCH (:`{j['from_label']}`)-[r:`{j['rel_type']}`]->(:`{j['to_label']}`) RETURN count(r) AS c"
            ).single()["c"]
            cur.execute(
                sql.SQL("SELECT {}, {} FROM {}.{} WHERE {} IS NOT NULL AND {} IS NOT NULL ORDER BY random() LIMIT %s").format(
                    sql.Identifier(j["from_via"]), sql.Identifier(j["to_via"]),
                    sql.Identifier(schema), sql.Identifier(name),
                    sql.Identifier(j["from_via"]), sql.Identifier(j["to_via"]),
                ),
                (args.sample,),
            )
            samples = [[lg.to_neo4j_value(a), lg.to_neo4j_value(b)] for a, b in cur.fetchall()]
            found = session.run(
                f"UNWIND $pairs AS p "
                f"MATCH (a:`{j['from_label']}` {{`{j['from_key_prop']}`: p[0]}})"
                f"-[:`{j['rel_type']}`]->"
                f"(b:`{j['to_label']}` {{`{j['to_key_prop']}`: p[1]}}) "
                "RETURN count(*) AS c",
                pairs=samples,
            ).single()["c"]
        else:
            pn = model["primary_node"]
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(name)))
            pg_count = cur.fetchone()[0]
            graph_count = session.run(f"MATCH (n:`{pn['label']}`) RETURN count(n) AS c").single()["c"]
            if pn.get("synthetic_key_from"):
                samples, found = [], None  # no natural key to spot-check
            else:
                keys = pn["key_props"]
                cur.execute(
                    sql.SQL("SELECT {} FROM {}.{} ORDER BY random() LIMIT %s").format(
                        sql.SQL(", ").join(sql.Identifier(k) for k in keys),
                        sql.Identifier(schema), sql.Identifier(name),
                    ),
                    (args.sample,),
                )
                samples = [[lg.to_neo4j_value(v) for v in row] for row in cur.fetchall()]
                key_match = " AND ".join(f"n.`{k}` = p[{i}]" for i, k in enumerate(keys))
                found = session.run(
                    f"UNWIND $keys AS p MATCH (n:`{pn['label']}`) WHERE {key_match} RETURN count(n) AS c",
                    keys=samples,
                ).single()["c"]

    out["counts"] = {"postgres": pg_count, "graph": graph_count, "match": pg_count == graph_count}
    if found is None:
        out["sample"] = {"skipped": "synthetic row keys — no natural key to spot-check"}
        ok = out["counts"]["match"]
    else:
        out["sample"] = {"checked": len(samples), "found": found, "missing": len(samples) - found}
        ok = out["counts"]["match"] and out["sample"]["missing"] == 0
    out["ok"] = ok
    print(json.dumps(out, indent=2, default=str))
    if not ok:
        sys.exit(3)


def cmd_run(args, model, env):
    if model.get("mode") == "join_table":
        # join tables are cheap: always a full idempotent re-merge
        total, counts, rels = lg.load_join(model, args.dsn, env, args.batch_size)
        print("===RESULT_JSON===")
        print(json.dumps({"synced": total, "mode": "join_table:full", "node_counts": counts, "relationships": rels}))
        return

    schema, name = lg.split_table(model["source"]["table"])
    table_key = f"{schema}.{name}"
    state = read_state()
    col = args.updated_column
    high_water = state.get(table_key, {}).get("high_water") if col and state.get(table_key, {}).get("column") == col else None

    if not col or high_water is None:
        mode = "full"
        total, counts, rels = lg.load(model, args.dsn, env, args.batch_size, wipe_label=False)
    else:
        mode = f"incremental(>= {high_water})"
        total = _incremental_load(model, args.dsn, env, args.batch_size, col, high_water)
        counts = rels = None

    if col:
        with pg_connect(args.dsn) as pg, pg.cursor() as cur:
            cur.execute(sql.SQL("SELECT max({}) FROM {}.{}").format(
                sql.Identifier(col), sql.Identifier(schema), sql.Identifier(name)))
            new_hw = cur.fetchone()[0]
        state[table_key] = {
            "column": col,
            "high_water": str(new_hw) if new_hw is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_state(state)

    print("===RESULT_JSON===")
    print(json.dumps({
        "synced": total, "mode": mode,
        **({"node_counts": counts, "relationships": rels} if counts else {}),
        **({"new_high_water": state.get(table_key, {}).get("high_water")} if col else {}),
        "note": "deletes are not propagated; use load_graph.py --wipe-label for a clean rebuild",
    }, default=str))


def _incremental_load(model, pg_dsn, env, batch_size, col, high_water):
    """Stream only rows with col >= high_water through the standard MERGE."""
    pn = model["primary_node"]
    schema, name = lg.split_table(model["source"]["table"])
    colnames = [c["name"] for c in model["columns"]]
    rel_cols = [r["via_column"] for r in model["relationships"]]
    row_cypher = lg.build_row_cypher(model)

    driver = GraphDatabase.driver(
        env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]), warn_notification_severity="OFF"
    )
    pg = pg_connect(pg_dsn)
    total = 0
    try:
        with driver.session() as session:
            lg.make_constraints(session, model)
            with pg.cursor(name="pg_sync_stream") as cur:
                cur.itersize = 5000
                cur.execute(
                    sql.SQL("SELECT {} FROM {}.{} WHERE {} >= %s").format(
                        sql.SQL(", ").join(sql.Identifier(c) for c in colnames),
                        sql.Identifier(schema), sql.Identifier(name), sql.Identifier(col),
                    ),
                    (high_water,),
                )
                buf = []
                while True:
                    batch = cur.fetchmany(5000)
                    if not batch:
                        break
                    for rec in batch:
                        row = {k: lg.to_neo4j_value(v) for k, v in zip(colnames, rec, strict=True)}
                        keyed = (
                            {"_row_key": lg.synth_key(row, pn["synthetic_key_from"])}
                            if pn.get("synthetic_key_from")
                            else {k: row.get(k) for k in pn["key_props"]}
                        )
                        rec_out = {**keyed, "props": {c: row.get(c) for c in pn["property_columns"]}}
                        for rc in rel_cols:
                            rec_out[rc] = row.get(rc)
                        buf.append(rec_out)
                        if len(buf) >= batch_size:
                            session.run(row_cypher, rows=buf)
                            total += len(buf)
                            buf = []
                if buf:
                    session.run(row_cypher, rows=buf)
                    total += len(buf)
    finally:
        pg.close()
        driver.close()
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default="model.json")
    common.add_argument("--dsn", required=True)
    common.add_argument("--env-file", default=".neo4j.env")

    v = sub.add_parser("verify", parents=[common])
    v.add_argument("--sample", type=int, default=100)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("run", parents=[common])
    r.add_argument("--updated-column", default=None, help="timestamp/serial column for incremental sync")
    r.add_argument("--batch-size", type=int, default=1000)
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    with open(args.model) as f:
        model = json.load(f)
    env = lg.load_env_file(args.env_file)
    args.func(args, model, env)


if __name__ == "__main__":
    main()

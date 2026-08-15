#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg[binary]>=3.2",
# ]
# ///
"""
Inspect a PostgreSQL table and auto-infer a property-graph model for Neo4j.

The goal is to turn one flat SQL table into a graph that is actually worth
visualizing -- not just a pile of disconnected row-nodes. We do that by looking
at three signals that PostgreSQL already knows about or that we can measure:

  1. Primary key      -> the identity of the "row node" (a unique node key).
  2. Foreign keys     -> real relationships to nodes in the referenced tables.
  3. Low-cardinality   -> categorical columns (status, category, department, ...)
     text columns         become their own "dimension" nodes so many rows fan
                           into a few shared hubs. This is what makes the graph
                           look like a graph instead of a star of orphans.

Everything else stays as a property on the row node.

Output: a model.json describing node labels, keys, properties, and relationships.
It is intentionally human-readable so a user can eyeball or hand-tune it before
loading. Run load_graph.py next to actually build the graph.

Usage:
  pg_inspect.py --dsn postgresql://user:pass@host:5432/db --table public.orders \
                --out model.json
"""

import argparse
import json
import re
import sys

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.exit("psycopg is required — run this script via `uv run` so its inline dependencies are provisioned")


IRREGULAR = {
    "people": "Person",
    "children": "Child",
    "men": "Man",
    "women": "Woman",
    "data": "Datum",
    "indices": "Index",
}
# words that look plural but are singular -- never strip the trailing 's'
KEEP_AS_IS = {
    "status",
    "series",
    "species",
    "news",
    "bonus",
    "virus",
    "campus",
    "analysis",
    "basis",
    "address",
    "class",
    "process",
    "access",
}


def pascal_singular(name):
    """orders -> Order, order_items -> OrderItem, status -> Status."""
    parts = re.split(r"[_\s]+", name.strip().lower())
    words = []
    for p in parts:
        if not p:
            continue
        if p in IRREGULAR:
            words.append(IRREGULAR[p])
            continue
        if len(p) <= 2 or p in KEEP_AS_IS or p.endswith(("us", "is", "ss", "sis")):
            pass  # short abbreviation ("cs", "id") / already singular / mass noun
        elif p.endswith("ies") and len(p) > 3:
            p = p[:-3] + "y"
        elif p.endswith("ses") and len(p) > 3:
            p = p[:-2]
        elif p.endswith("s") and len(p) > 1:
            p = p[:-1]
        words.append(p.capitalize())
    return "".join(words) or "Row"


def rel_type_from_column(col):
    """user_id -> HAS_USER, category -> IN_CATEGORY-ish. Kept simple + readable."""
    base = re.sub(r"_id$|_key$|_fk$", "", col.lower())
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return "HAS_" + base.upper() if base else "REFERS_TO"


def rel_type_from_table(name):
    """order_items -> ORDER_ITEM (a join table becomes a relationship type)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", pascal_singular(name)).upper()


def split_table(table):
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = "public", table
    return schema, name


def fetch_columns(cur, schema, name):
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, name),
    )
    return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in cur.fetchall()]


def fetch_primary_key(cur, schema, name):
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (f'"{schema}"."{name}"',),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_foreign_keys(cur, schema, name):
    # pg_constraint with pairwise unnest(conkey, confkey) keeps local/referenced
    # columns aligned by position; the information_schema equivalent cross-joins
    # the columns of a composite FK and yields wrong pairs.
    cur.execute(
        """
        SELECT c.conname   AS constraint_name,
               a.attname   AS column_name,
               fn.nspname  AS ref_schema,
               fc.relname  AS ref_table,
               fa.attname  AS ref_column
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS u(attnum, fattnum)
        JOIN pg_attribute a  ON a.attrelid = c.conrelid  AND a.attnum = u.attnum
        JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = u.fattnum
        JOIN pg_class fc     ON fc.oid = c.confrelid
        JOIN pg_namespace fn ON fn.oid = fc.relnamespace
        WHERE c.contype = 'f' AND c.conrelid = %s::regclass
        """,
        (f'"{schema}"."{name}"',),
    )
    return [
        {"constraint": cn, "column": col, "ref_schema": rs, "ref_table": rt, "ref_column": rc}
        for cn, col, rs, rt, rc in cur.fetchall()
    ]


TEXTY = {"character varying", "varchar", "text", "char", "character", "boolean", "name", "citext", "uuid"}


def measure_cardinality(cur, schema, name, columns, row_count, max_distinct):
    """Find categorical columns worth promoting to dimension nodes."""
    cats = {}
    if row_count == 0:
        return cats
    for c in columns:
        if c["type"].lower() not in TEXTY:
            continue
        col_ident = sql.SQL("{}").format(sql.Identifier(c["name"]))
        q = sql.SQL("SELECT COUNT(DISTINCT {}) FROM {}.{}").format(
            col_ident, sql.Identifier(schema), sql.Identifier(name)
        )
        cur.execute(q)
        distinct = cur.fetchone()[0] or 0
        # categorical if it repeats (not unique) and has few distinct values
        if 1 < distinct <= max_distinct and distinct < row_count:
            cats[c["name"]] = distinct
    return cats


def build_model(cur, table, max_distinct_abs, max_distinct_ratio):
    schema, name = split_table(table)

    # confirm table exists
    cur.execute("SELECT to_regclass(%s)", (f'"{schema}"."{name}"',))
    if cur.fetchone()[0] is None:
        raise SystemExit(f"Table {schema}.{name} not found in the database.")

    columns = fetch_columns(cur, schema, name)
    pk = fetch_primary_key(cur, schema, name)
    fks = fetch_foreign_keys(cur, schema, name)
    fk_cols = {f["column"] for f in fks}

    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(name)))
    row_count = cur.fetchone()[0]

    # ── Join-table detection ────────────────────────────────────────────────
    # A table whose primary key is made of exactly two FK columns is a pure
    # link between two entities (order_items, enrollments, friendships).
    # Modeling its rows as *relationships with properties* — not nodes —
    # is the single biggest "looks like a real graph now" upgrade.
    fk_by_col = {f["column"]: f for f in fks}
    pk_fk_cols = [c for c in pk if c in fk_by_col]
    distinct_constraints = {fk_by_col[c]["constraint"] for c in pk_fk_cols}
    # Require the two PK columns to come from two DISTINCT FK constraints:
    # the two halves of a single composite FK are a detail table pointing at
    # one parent, not a join table.
    if pk and len(pk) == 2 and len(pk_fk_cols) == 2 and len(distinct_constraints) == 2:
        a, b = fk_by_col[pk_fk_cols[0]], fk_by_col[pk_fk_cols[1]]
        rel_props = [c["name"] for c in columns if c["name"] not in set(pk)]
        return {
            "source": {"table": f"{schema}.{name}", "row_count": row_count},
            "mode": "join_table",
            "join_relationship": {
                "rel_type": rel_type_from_table(name),
                "from_label": pascal_singular(a["ref_table"]),
                "from_key_prop": a["ref_column"],
                "from_via": a["column"],
                "to_label": pascal_singular(b["ref_table"]),
                "to_key_prop": b["ref_column"],
                "to_via": b["column"],
                "property_columns": rel_props,
            },
            "columns": columns,
            "notes": "Auto-inferred join table: rows become relationships. Edit rel_type or the endpoint labels, then run load_graph.py.",
        }

    # a column is "categorical" if it has few distinct values. Use a floor of 20
    # so small tables still promote obvious dimensions (status, type, ...), but
    # cap by an absolute max and by a ratio of the row count for large tables.
    max_distinct = min(max_distinct_abs, max(20, int(row_count * max_distinct_ratio)))
    cats = measure_cardinality(cur, schema, name, columns, row_count, max_distinct)
    # a FK column is already handled as a relationship; don't also make it a dimension
    cats = {k: v for k, v in cats.items() if k not in fk_cols and k not in pk}

    node_label = pascal_singular(name)

    # node key: PK if present, else synthetic row hash built at load time
    if pk:
        key_props = pk
        synthetic_key = None
    else:
        key_props = ["_row_key"]
        synthetic_key = [c["name"] for c in columns]

    # properties on the primary node = all columns except those turned into
    # dimension relationships and except FK columns (once a FK becomes an
    # arrow, keeping the raw id column too is double bookkeeping) — PK
    # columns always stay
    dimension_cols = set(cats)
    prop_cols = [
        c["name"]
        for c in columns
        if c["name"] not in dimension_cols and (c["name"] not in fk_cols or c["name"] in pk)
    ]

    relationships = []
    for f in fks:
        self_ref = f["ref_table"] == name and f["ref_schema"] == schema
        relationships.append(
            {
                "kind": "foreign_key",
                "from_label": node_label,
                "rel_type": rel_type_from_column(f["column"]),
                "to_label": pascal_singular(f["ref_table"]),
                "to_key_prop": f["ref_column"],
                "via_column": f["column"],
                **({"self_reference": True} if self_ref else {}),
            }
        )
    for col, distinct in cats.items():
        relationships.append(
            {
                "kind": "dimension",
                "from_label": node_label,
                "rel_type": rel_type_from_column(col),
                "to_label": pascal_singular(col),
                "to_key_prop": "value",
                "via_column": col,
                "distinct_values": distinct,
            }
        )

    return {
        "source": {"table": f"{schema}.{name}", "row_count": row_count},
        "mode": "node_table",
        "primary_node": {
            "label": node_label,
            "key_props": key_props,
            "synthetic_key_from": synthetic_key,
            "property_columns": prop_cols,
        },
        "relationships": relationships,
        "columns": columns,
        "notes": "Auto-inferred. Edit rel_type / to_label / drop relationships as you like, then run load_graph.py.",
    }


def arrows_export(model):
    """The model as an Arrows.app diagram (https://arrows.app -> Import),
    so the user can hand-tune the model visually before loading."""
    import math

    nodes, rels = [], []
    if model.get("mode") == "join_table":
        j = model["join_relationship"]
        labels = [(j["from_label"], [j["from_key_prop"]]), (j["to_label"], [j["to_key_prop"]])]
        for i, (label, props) in enumerate(labels):
            nodes.append(
                {
                    "id": f"n{i}",
                    "position": {"x": i * 400.0, "y": 0.0},
                    "caption": label,
                    "labels": [label],
                    "properties": {p: "" for p in props},
                    "style": {},
                }
            )
        rels.append(
            {
                "id": "r0",
                "fromId": "n0",
                "toId": "n1",
                "type": j["rel_type"],
                "properties": {p: "" for p in j["property_columns"]},
                "style": {},
            }
        )
    else:
        pn = model["primary_node"]
        nodes.append(
            {
                "id": "n0",
                "position": {"x": 0.0, "y": 0.0},
                "caption": pn["label"],
                "labels": [pn["label"]],
                "properties": {p: "" for p in pn["property_columns"][:12]},
                "style": {},
            }
        )
        seen = {pn["label"]: "n0"}
        others = [r for r in model["relationships"]]
        for i, r in enumerate(others):
            if r["to_label"] not in seen:
                nid = f"n{len(seen)}"
                angle = 2 * math.pi * i / max(len(others), 1)
                nodes.append(
                    {
                        "id": nid,
                        "position": {"x": 420.0 * math.cos(angle), "y": 420.0 * math.sin(angle)},
                        "caption": r["to_label"],
                        "labels": [r["to_label"]],
                        "properties": {r["to_key_prop"]: ""},
                        "style": {},
                    }
                )
                seen[r["to_label"]] = nid
            rels.append(
                {
                    "id": f"r{i}",
                    "fromId": "n0",
                    "toId": seen[r["to_label"]],
                    "type": r["rel_type"],
                    "properties": {},
                    "style": {},
                }
            )
    return {"nodes": nodes, "relationships": rels, "style": {}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="postgres DSN, e.g. postgresql://user:pass@localhost:5432/db")
    ap.add_argument("--table", required=True, help="table name, optionally schema-qualified (public.orders)")
    ap.add_argument("--out", default="model.json")
    ap.add_argument(
        "--max-distinct-abs", type=int, default=50, help="max distinct values for a column to count as categorical"
    )
    ap.add_argument(
        "--max-distinct-ratio",
        type=float,
        default=0.1,
        help="max distinct/row ratio for a column to count as categorical",
    )
    args = ap.parse_args()

    conn = psycopg.connect(args.dsn)
    try:
        with conn.cursor() as cur:
            model = build_model(cur, args.table, args.max_distinct_abs, args.max_distinct_ratio)
    finally:
        conn.close()

    with open(args.out, "w") as f:
        json.dump(model, f, indent=2)
    arrows_path = re.sub(r"\.json$", "", args.out) + ".arrows.json"
    with open(arrows_path, "w") as f:
        json.dump(arrows_export(model), f, indent=2)

    print(f"Model written to {args.out}")
    print(f"  Source table : {model['source']['table']} ({model['source']['row_count']} rows)")
    if model["mode"] == "join_table":
        j = model["join_relationship"]
        print("  Detected a JOIN TABLE — rows become relationships, not nodes:")
        print(f"    (:{j['from_label']})-[:{j['rel_type']} {{{', '.join(j['property_columns'])}}}]->(:{j['to_label']})")
    else:
        pn = model["primary_node"]
        print(f"  Primary node : (:{pn['label']}) keyed on {pn['key_props']}")
        print(f"  Properties   : {len(pn['property_columns'])} columns (FK id columns folded into relationships)")
        print(f"  Relationships: {len(model['relationships'])}")
        for r in model["relationships"]:
            extra = " (self-referencing — hierarchy!)" if r.get("self_reference") else ""
            print(f"    (:{r['from_label']})-[:{r['rel_type']}]->(:{r['to_label']})  [{r['kind']}]{extra}")
    print(f"  Visual editor: {arrows_path} — import at https://arrows.app to hand-tune the model")


if __name__ == "__main__":
    main()

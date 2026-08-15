#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j>=5.28,<6",
# ]
# ///
"""Execute ONE read-only Cypher query against the local Neo4j; JSON to stdout.

Safety layers (server-side, no regex guessing):
  1. EXPLAIN pre-flight: the query is compiled (not executed) first, so syntax
     errors and unknown-clause mistakes surface before anything runs.
  2. READ access mode on the session: Neo4j itself rejects
     CREATE/MERGE/DELETE/SET at execution.
  3. Server-side transaction timeout (--timeout, default 30s): a runaway query
     is killed by the database, not the client.

Assumes graph_context.py ran first (container up, env file present).

Run with:  uv run run_cypher.py --cypher 'MATCH ...' [--max-rows 200] [--timeout 30]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path as FsPath

DEFAULT_ENV_FILE = ".neo4j.env"
MAX_ROWS_CAP = 1000


def read_env_file(path: str) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with FsPath(path).open() as f:
            for raw in f:
                line = raw.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    info[k] = v
    except FileNotFoundError:
        pass
    return info


def serialize(value):
    """Flatten driver graph types into plain JSON the model can read."""
    from neo4j.graph import Node, Path, Relationship

    if isinstance(value, Node):
        return {"_node": sorted(value.labels), "props": dict(value)}
    if isinstance(value, Relationship):
        return {"_rel": value.type, "props": dict(value)}
    if isinstance(value, Path):
        return {
            "_path": [serialize(x) for pair in zip(value.nodes, value.relationships, strict=False) for x in pair]
            + [serialize(value.nodes[-1])]
        }
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cypher", help="query text; reads stdin if omitted")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=30.0, help="server-side transaction timeout, seconds")
    args = ap.parse_args()

    cypher = args.cypher or sys.stdin.read()
    if not cypher.strip():
        sys.exit("error: empty query")
    max_rows = min(MAX_ROWS_CAP, max(1, args.max_rows))
    if args.timeout <= 0:
        sys.exit("error: --timeout must be > 0 (0 would disable the server-side cap)")

    info = read_env_file(args.env_file)
    if not info.get("NEO4J_URI"):
        sys.exit(f"error: {args.env_file} missing or incomplete — run graph_context.py first")

    from neo4j import READ_ACCESS, GraphDatabase, Query
    from neo4j.exceptions import Neo4jError

    started = time.monotonic()
    try:
        with (
            GraphDatabase.driver(info["NEO4J_URI"], auth=(info["NEO4J_USER"], info["NEO4J_PASSWORD"])) as driver,
            driver.session(default_access_mode=READ_ACCESS) as session,
        ):
            # Pre-flight: compile without executing. Catches syntax errors (and
            # planner-level problems) before any work happens. Skip when the
            # caller already sent EXPLAIN/PROFILE, or a CYPHER options prefix
            # (the options must precede EXPLAIN, so prepending would be
            # invalid; execution safety still holds via access mode + timeout).
            if not cypher.lstrip().upper().startswith(("EXPLAIN", "PROFILE", "CYPHER")):
                try:
                    session.run(Query("EXPLAIN " + cypher, timeout=args.timeout)).consume()
                except Neo4jError as exc:
                    print(json.dumps({"error": f"{exc.code}: {exc.message}", "stage": "explain"}))
                    sys.exit(1)

            result = session.run(Query(cypher, timeout=args.timeout))
            columns = result.keys()
            rows, truncated = [], False
            for record in result:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append([serialize(v) for v in record.values()])
    except Neo4jError as exc:
        print(json.dumps({"error": f"{exc.code}: {exc.message}", "stage": "execute"}))
        sys.exit(1)

    print(
        json.dumps(
            {
                "columns": list(columns),
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

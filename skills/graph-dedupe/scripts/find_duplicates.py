#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j>=5.28,<6",
#     "rapidfuzz>=3.9",
# ]
# ///
"""Find likely duplicate nodes and mark them with pending SAME_AS edges.

GENTLE BY DESIGN: this script never merges, never deletes, and never touches
user data. Its only writes are `SAME_AS` relationships tagged
`source: 'pg-graph-toolkit-dedupe'`, carrying a confidence score and a
`status: 'pending'` for human review. `--clear` removes exactly those edges
and nothing else. `--dry-run` writes nothing at all.

The funnel (cheap first, precision-first thresholds):
  1. exact match after normalization (lowercase, strip punctuation/spaces)
     -> confidence 0.99
  2. fuzzy match (rapidfuzz token_sort_ratio) within first-letter blocks
     (recall limit: values differing in their first letter — "The ACME" vs
     "ACME" — land in different blocks and are not compared)
     -> confidence = score/100, only pairs >= --threshold-review
Bands: >= --threshold-high (default 0.95) = auto-merge candidates;
between review and high = needs human judgment; below review = ignored.
False merges cost far more than missed ones — thresholds err high.

Run with:  uv run find_duplicates.py --label Customer --property name [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_ENV_FILE = ".neo4j.env"
SOURCE_TAG = "pg-graph-toolkit-dedupe"
MAX_PAIRS_PER_NODE = 3


def read_env_file(path: str) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with Path(path).open() as f:
            for raw in f:
                line = raw.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    info[k] = v
    except FileNotFoundError:
        pass
    return info


def normalize(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", v.lower())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--property", required=True, dest="prop")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--threshold-review", type=float, default=0.85)
    ap.add_argument("--threshold-high", type=float, default=0.95)
    ap.add_argument("--limit", type=int, default=5000, help="max nodes scanned")
    ap.add_argument("--dry-run", action="store_true", help="report pairs, write nothing")
    ap.add_argument("--clear", action="store_true", help="remove this tool's SAME_AS edges for the label and exit")
    args = ap.parse_args()
    if not (0 < args.threshold_review <= args.threshold_high <= 1):
        sys.exit("error: need 0 < --threshold-review <= --threshold-high <= 1")

    info = read_env_file(args.env_file)
    if not info.get("NEO4J_URI"):
        sys.exit(f"error: {args.env_file} missing or incomplete — run graph_context.py first")

    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError
    from rapidfuzz import fuzz

    lbl = args.label.replace("`", "``")
    prop = args.prop.replace("`", "``")

    try:
        with GraphDatabase.driver(info["NEO4J_URI"], auth=(info["NEO4J_USER"], info["NEO4J_PASSWORD"])) as driver:
            with driver.session() as session:
                if args.clear:
                    res = session.run(
                        # directed: an undirected pattern would match (and count) each edge twice
                        f"MATCH (:`{lbl}`)-[r:SAME_AS {{source: $src}}]->(:`{lbl}`) DELETE r RETURN count(r) AS c",
                        src=SOURCE_TAG,
                    ).single()
                    print(json.dumps({"cleared_edges": res["c"], "label": args.label}))
                    return

                nodes = session.run(
                    f"MATCH (n:`{lbl}`) WHERE n.`{prop}` IS NOT NULL "
                    f"RETURN elementId(n) AS id, toString(n.`{prop}`) AS v LIMIT $lim",
                    lim=args.limit,
                ).data()

                pairs: dict[tuple[str, str], dict] = {}
                per_node: dict[str, int] = defaultdict(int)

                def add_pair(a, b, conf, method):
                    key = tuple(sorted((a["id"], b["id"])))
                    if key in pairs or per_node[a["id"]] >= MAX_PAIRS_PER_NODE or per_node[b["id"]] >= MAX_PAIRS_PER_NODE:
                        return
                    pairs[key] = {
                        "a_id": key[0], "b_id": key[1],
                        "a_value": a["v"] if a["id"] == key[0] else b["v"],
                        "b_value": b["v"] if a["id"] == key[0] else a["v"],
                        "confidence": round(conf, 4), "method": method,
                    }
                    per_node[a["id"]] += 1
                    per_node[b["id"]] += 1

                # 1) exact after normalization
                by_norm: dict[str, list] = defaultdict(list)
                for n in nodes:
                    norm = normalize(n["v"])
                    if norm:
                        by_norm[norm].append(n)
                for group in by_norm.values():
                    for i in range(len(group)):
                        for j in range(i + 1, len(group)):
                            add_pair(group[i], group[j], 0.99, "exact-normalized")

                # 2) fuzzy within first-letter blocks (skip already-paired)
                blocks: dict[str, list] = defaultdict(list)
                for n in nodes:
                    norm = normalize(n["v"])
                    if norm:
                        blocks[norm[0]].append(n)
                for block in blocks.values():
                    for i in range(len(block)):
                        for j in range(i + 1, len(block)):
                            a, b = block[i], block[j]
                            if tuple(sorted((a["id"], b["id"]))) in pairs:
                                continue
                            score = fuzz.token_sort_ratio(a["v"], b["v"]) / 100.0
                            if score >= args.threshold_review:
                                add_pair(a, b, score, "fuzzy-token-sort")

                written = 0
                if pairs and not args.dry_run:
                    session.run(
                        "UNWIND $pairs AS p "
                        "MATCH (a) WHERE elementId(a) = p.a_id "
                        "MATCH (b) WHERE elementId(b) = p.b_id "
                        # scoped by source: never matches/hijacks a user's own SAME_AS edge
                        "MERGE (a)-[r:SAME_AS {source: $src}]->(b) "
                        "SET r.confidence = p.confidence, r.method = p.method, "
                        "    r.status = coalesce(r.status, 'pending'), "
                        "    r.property = $prop, r.source = $src",
                        pairs=list(pairs.values()), prop=args.prop, src=SOURCE_TAG,
                    ).consume()
                    written = len(pairs)
    except Neo4jError as exc:
        print(json.dumps({"error": f"{exc.code}: {exc.message}"}))
        sys.exit(1)

    plist = sorted(pairs.values(), key=lambda p: -p["confidence"])
    print(
        json.dumps(
            {
                "label": args.label,
                "property": args.prop,
                "nodes_scanned": len(nodes),
                "auto_merge_candidates": [p for p in plist if p["confidence"] >= args.threshold_high],
                "needs_review": [p for p in plist if p["confidence"] < args.threshold_high],
                "edges_written": written,
                "dry_run": args.dry_run,
                "review_queue_cypher": (
                    f"MATCH (a:`{lbl}`)-[r:SAME_AS {{status: 'pending', source: 'pg-graph-toolkit-dedupe'}}]->(b:`{lbl}`)\n"
                    f"RETURN a.`{prop}` AS a, b.`{prop}` AS b, r.confidence AS confidence, r.method AS method\n"
                    "ORDER BY confidence DESC"
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

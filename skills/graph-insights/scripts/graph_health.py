#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j>=5.28,<6",
# ]
# ///
"""Pure-Cypher health & review pack for the loaded graph; JSON to stdout.

Read-only (READ access mode). Collects the raw material the graph-insights
skill narrates: per-label counts, top hubs by degree, orphan counts, degree
concentration, constraint coverage, and deterministic model-review findings
(supernode dimensions, generic names, mixed key shapes under one label).
Interpretation and SQL-analogy explanations happen in the skill, not here.

Assumes the graph is up and .neo4j.env exists (run graph_context.py first if
unsure). Labels above --big-label-cap nodes skip the expensive degree scans.

Run with:  uv run graph_health.py [--env-file .neo4j.env]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ENV_FILE = ".neo4j.env"
DIMENSION_MAX_NODES = 50  # same convention as graph_context.py / the loader
SUPERNODE_DEGREE = 100_000  # a dimension hub this connected deserves a warning
GENERIC_NAMES = {"Entity", "Node", "Item", "Object", "Data", "RELATED_TO", "CONNECTED_TO", "HAS", "REL"}


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--big-label-cap", type=int, default=500_000, help="skip degree scans for labels above this size")
    args = ap.parse_args()

    info = read_env_file(args.env_file)
    if not info.get("NEO4J_URI"):
        sys.exit(f"error: {args.env_file} missing or incomplete — run graph_context.py first")

    from neo4j import GraphDatabase, RoutingControl
    from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable

    def bt(label: str) -> str:
        return label.replace("`", "``")

    out: dict = {"labels": [], "review": []}
    try:
        _collect(info, GraphDatabase, RoutingControl, bt, out, args)
    except (ServiceUnavailable, AuthError) as exc:
        print(json.dumps({"error": f"cannot reach Neo4j: {exc}", "hint": "run graph_context.py first (it starts a stopped container and waits for bolt)"}))
        sys.exit(1)
    except Neo4jError as exc:
        print(json.dumps({"error": f"{exc.code}: {exc.message}"}))
        sys.exit(1)

    print(json.dumps(out, indent=2, default=str))


def _collect(info, GraphDatabase, RoutingControl, bt, out, args) -> None:
    with GraphDatabase.driver(info["NEO4J_URI"], auth=(info["NEO4J_USER"], info["NEO4J_PASSWORD"])) as driver:

        def rows(query: str, **params) -> list[dict]:
            # RoutingControl.READ = read access mode on execute_query
            recs, _, _ = driver.execute_query(query, params, database_="neo4j", routing_=RoutingControl.READ)
            return [r.data() for r in recs]

        labels = rows("MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY count DESC")
        rel_types = rows("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC")
        out["relationship_types"] = rel_types
        out["total_relationships"] = sum(r["c"] for r in rel_types)

        for entry in labels:
            label, count = entry["label"], entry["count"]
            item: dict = {"label": label, "count": count}

            # property-key shapes: DISJOINT shapes under one label usually mean
            # two different loads collided on the same label. Shapes that are
            # subsets of a bigger shape are collapsed first — NULL columns and
            # FK stub nodes legitimately produce subset shapes and are not
            # collisions. Grouping happens client-side (sorted tuples) —
            # Cypher key order isn't guaranteed.
            from collections import Counter

            sampled = rows(f"MATCH (n:`{bt(label)}`) WITH n LIMIT 2000 RETURN keys(n) AS ks")
            shape_counts = Counter(frozenset(s["ks"]) for s in sampled)
            maximal: dict[frozenset, int] = {}
            for shape, n in sorted(shape_counts.items(), key=lambda kv: -len(kv[0])):
                for bigger in maximal:
                    if shape <= bigger:
                        maximal[bigger] += n
                        break
                else:
                    maximal[shape] = n
            item["key_shapes"] = [
                {"shape": sorted(shape), "nodes": n}
                for shape, n in sorted(maximal.items(), key=lambda kv: -kv[1])[:6]
            ]

            if count <= args.big_label_cap:
                hubs = rows(
                    f"MATCH (n:`{bt(label)}`) WITH n, COUNT {{ (n)--() }} AS d "
                    "ORDER BY d DESC LIMIT 5 "
                    "RETURN d AS degree, "
                    "[k IN ['id', 'name', 'value', '_row_key'] WHERE n[k] IS NOT NULL | k + '=' + toString(n[k])] AS ident"
                )
                item["top_degrees"] = hubs
                orphans = rows(f"MATCH (n:`{bt(label)}`) WHERE COUNT {{ (n)--() }} = 0 RETURN count(n) AS c")
                item["orphans"] = orphans[0]["c"]
            else:
                item["degree_scan"] = "skipped (label above --big-label-cap)"
            out["labels"].append(item)

        out["constraints"] = rows(
            "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties RETURN name, type, labelsOrTypes, properties"
        )

        # ── deterministic review findings ──────────────────────────────────
        # (label-level check only: the graph alone doesn't know each label's
        # intended MERGE key, so property-level matching would be guesswork)
        constrained_labels = {lbl for c in out["constraints"] for lbl in (c["labelsOrTypes"] or [])}
        for r in rel_types:
            if r["t"] in GENERIC_NAMES:
                out["review"].append(
                    {"kind": "generic_name", "type": r["t"], "detail": "generic relationship type carries no meaning in queries"}
                )
        for item in out["labels"]:
            label, count = item["label"], item["count"]
            if label in GENERIC_NAMES:
                out["review"].append(
                    {"kind": "generic_name", "label": label, "detail": "generic label name carries no meaning in queries"}
                )
            if len(item.get("key_shapes", [])) > 1:
                out["review"].append(
                    {
                        "kind": "mixed_key_shapes",
                        "label": label,
                        "detail": f"{len(item['key_shapes'])} DISJOINT property shapes under one label "
                        "(sampled; subset shapes from NULLs/stub nodes already collapsed) — "
                        "usually two different loads colliding on the same label name",
                        "shapes": item["key_shapes"],
                    }
                )
            if count > 1000 and label not in constrained_labels:
                out["review"].append(
                    {
                        "kind": "missing_constraint",
                        "label": label,
                        "detail": "no uniqueness constraint — MERGEs on this label full-scan (like a table with no index)",
                    }
                )
            if count <= DIMENSION_MAX_NODES:
                top = (item.get("top_degrees") or [{}])[0].get("degree", 0)
                if top > SUPERNODE_DEGREE:
                    out["review"].append(
                        {
                            "kind": "supernode_dimension",
                            "label": label,
                            "detail": f"dimension hub with degree {top} — at this scale a label "
                            "(e.g. :Active) beats a hub node; every query through it funnels one node",
                        }
                    )


if __name__ == "__main__":
    main()

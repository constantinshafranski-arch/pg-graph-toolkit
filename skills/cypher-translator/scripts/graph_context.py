#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j>=5.28,<6",
# ]
# ///
"""Ensure the local Neo4j is reachable and emit a LIVE schema digest as JSON.

Discovery ladder (no pre-baked schema, ever):
  1. Read connection details from the env file (.neo4j.env, written by the
     sql-to-neo4j skill's neo4j_manager.py).
  2. Env file missing -> `docker inspect` the container for NEO4J_AUTH + port
     bindings and rewrite the env file so the next call is instant.
  3. Container stopped -> `docker start` it and wait for bolt.
  4. No container at all -> exit 2: there is no graph to translate against;
     run the sql-to-neo4j skill first.

The digest is introspected fresh on EVERY call (labels, relationship types,
property keys, dimension values, counts) — saved model.json files from past
loads are never read; they go stale the moment the graph is reloaded.

Run with:  uv run graph_context.py [--env-file .neo4j.env]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_ENV_FILE = ".neo4j.env"
DEFAULT_CONTAINER = "graph-neo4j"
# A label this small is a "dimension" (status/category hub the loader created);
# its full value list fits in a prompt and is what grounds translation.
DIMENSION_MAX_NODES = 50
PROPERTY_SAMPLE = 100  # nodes sampled per label for property-key discovery


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


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def inspect_container(name: str) -> dict[str, str]:
    """Rebuild connection info from the container itself (env file lost)."""
    r = docker("inspect", name)
    if r.returncode != 0:
        return {}
    data = json.loads(r.stdout)[0]
    info: dict[str, str] = {"NEO4J_CONTAINER": name}
    for e in data.get("Config", {}).get("Env", []) or []:
        if e.startswith("NEO4J_AUTH=") and "/" in e:
            user, password = e.split("=", 1)[1].split("/", 1)
            info["NEO4J_USER"] = user
            info["NEO4J_PASSWORD"] = password
    for cport, binds in (data.get("NetworkSettings", {}).get("Ports", {}) or {}).items():
        if binds:
            if cport.startswith("7687"):
                info["NEO4J_URI"] = f"bolt://localhost:{binds[0]['HostPort']}"
            elif cport.startswith("7474"):
                info["NEO4J_HTTP"] = f"http://localhost:{binds[0]['HostPort']}"
    return info


def container_state(name: str) -> str | None:
    r = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}")
    out = (r.stdout or "").strip()
    if not out:
        return None
    return "running" if out.startswith("running") else "stopped"


def write_env_file(path: str, info: dict[str, str]) -> None:
    keys = ("NEO4J_URI", "NEO4J_HTTP", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_CONTAINER")
    Path(path).write_text("\n".join(f"{k}={info[k]}" for k in keys if k in info) + "\n")


def ensure_up(env_file: str, container: str) -> dict[str, str]:
    info = read_env_file(env_file)
    name = info.get("NEO4J_CONTAINER", container)
    state = container_state(name)
    if state is None:
        print(
            f"error: no Neo4j container named '{name}' exists — there is no graph to "
            "translate against. Load one first with the sql-to-neo4j skill.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not (info.get("NEO4J_URI") and info.get("NEO4J_USER") and info.get("NEO4J_PASSWORD")):
        info = inspect_container(name)
        if not info.get("NEO4J_PASSWORD"):
            sys.exit(f"error: could not recover credentials from container '{name}'.")
        write_env_file(env_file, info)
    if state == "stopped":
        r = docker("start", name)
        if r.returncode != 0:
            sys.exit(f"error: docker start {name} failed: {r.stderr.strip()}")
    return info


def wait_bolt(driver, timeout: float = 90.0) -> None:
    from neo4j.exceptions import AuthError

    deadline = time.time() + timeout
    while True:
        try:
            driver.verify_connectivity()
        except AuthError:
            # Bad credentials never heal with retries — and retrying trips the
            # server's AuthenticationRateLimit. Fail fast with the fix.
            sys.exit(
                "error: Neo4j rejected the credentials. Delete the stale env file "
                "and rerun graph_context.py to recover them from the container."
            )
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(2)
        else:
            return


def introspect(driver, database: str = "neo4j") -> dict:
    from neo4j import RoutingControl

    def rows(query: str, **params) -> list[dict]:
        recs, _, _ = driver.execute_query(query, params, database_=database, routing_=RoutingControl.READ)
        return [r.data() for r in recs]

    def bt(label: str) -> str:
        """Backtick-escape a label for safe interpolation (labels can't be parameters)."""
        return label.replace("`", "``")

    labels = rows("MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label")
    rels = rows("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type")
    # Directed patterns (from)-[TYPE]->(to): what grounds arrow DIRECTION in
    # generated Cypher — the single most common translation mistake.
    patterns = rows(
        "MATCH (a)-[r]->(b) "
        "WITH labels(a)[0] AS `from`, type(r) AS `type`, labels(b)[0] AS `to`, count(*) AS `count` "
        "RETURN `from`, `type`, `to`, `count` ORDER BY `count` DESC LIMIT 200"
    )

    out_labels = []
    for entry in labels:
        label, count = entry["label"], entry["count"]
        props = rows(
            f"MATCH (n:`{bt(label)}`) WITH n LIMIT $sample UNWIND keys(n) AS k RETURN DISTINCT k ORDER BY k",
            sample=PROPERTY_SAMPLE,
        )
        item: dict = {"label": label, "count": count, "properties": [p["k"] for p in props]}
        if count <= DIMENSION_MAX_NODES:
            vals = rows(f"MATCH (n:`{bt(label)}`) RETURN n.value AS v ORDER BY v")
            values = [v["v"] for v in vals if v["v"] is not None]
            if values:
                item["dimension_values"] = values
        out_labels.append(item)

    fingerprint = hashlib.sha256("|".join(f"{e['label']}:{e['count']}" for e in labels).encode()).hexdigest()[:16]

    return {"labels": out_labels, "relationships": rels, "patterns": patterns, "graph_fingerprint": fingerprint}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    args = ap.parse_args()

    info = ensure_up(args.env_file, args.container)

    from neo4j import GraphDatabase

    with GraphDatabase.driver(info["NEO4J_URI"], auth=(info["NEO4J_USER"], info["NEO4J_PASSWORD"])) as driver:
        wait_bolt(driver)
        digest = introspect(driver)

    digest["connection"] = {
        "uri": info["NEO4J_URI"],
        "user": info["NEO4J_USER"],
        "container": info.get("NEO4J_CONTAINER", args.container),
        "env_file": args.env_file,
    }
    print(json.dumps(digest, indent=2, default=str))


if __name__ == "__main__":
    main()

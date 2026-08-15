# pg-graph-toolkit

**Your SQL table becomes a graph you can talk to.**

A [Claude Code](https://code.claude.com) plugin that carries a table in a local
Dockerized PostgreSQL all the way to a live, explorable Neo4j graph — and then
answers plain-English questions about it, grounded in the real schema, with
memory across follow-ups. Local-first: Postgres stays your source of truth; the
graph is a disposable mirror you can rebuild with one command.

## Skills

| Skill | What it does |
|---|---|
| `sql-to-neo4j` | Inspects a Postgres table, auto-infers a graph model (rows → nodes, FKs → relationships, low-cardinality columns → dimension hubs), brings up a local Neo4j Community container (reusing whatever exists), streams the rows in idempotently with constraints, and hands back a Neo4j Browser URL with a visualization query pre-loaded — or a fully offline single-file HTML snapshot of the graph (no Neo4j needed to view). |
| `cypher-translator` | Translates plain English into read-only Cypher grounded in a *live* introspection of the loaded graph, executes it safely (EXPLAIN pre-flight, server-enforced read-only, 30s query timeout), annotates answers with their SQL equivalents, and keeps a small conversation memory so follow-ups like "of those, only the top 3" work. |
| `graph-status` | Quick read-only checks: is Neo4j up, what's loaded, where's the Browser link — without waking the full translation flow. |
| `graph-insights` | Narrated health check and model review: hubs, orphans (which are data-quality findings about your source table), collisions, missing constraints — each finding explained with a SQL analogy and a click-to-run query. |
| `graph-dedupe` | Finds likely duplicate entities (precision-first fuzzy funnel) and marks them with reviewable `SAME_AS` edges. Never merges, never deletes — undo built in. |

## Requirements

- [Claude Code](https://code.claude.com) v2.1.129+ (for `${CLAUDE_SKILL_DIR}`
  substitution in `allowed-tools`)
- [`uv`](https://docs.astral.sh/uv/) on PATH — the bundled scripts are
  self-contained (PEP 723) and provision their own dependencies on first run
- Docker (for the local Postgres source and the Neo4j container). Note:
  a NEW Neo4j container fetches the APOC plugin at first start (one-time
  network access); pass `--no-plugins` to `neo4j_manager.py up` on offline
  machines, or `--with-gds` to add Graph Data Science CE

## Install

```shell
/plugin marketplace add constantinshafranski-arch/pg-graph-toolkit
/plugin install pg-graph-toolkit@costiash-plugins
```

Then just ask Claude things like:

> put my `orders` table into Neo4j

> show me the customers with the most orders in the graph

## Safety posture

- The Postgres side is **read-only by design** (SELECT / catalog queries only),
  and the docs insist on local instances — never point it at production.
- Graph queries from the translator run with the Neo4j driver's
  **READ access mode**, so the server itself rejects writes — plus an
  EXPLAIN pre-flight and a server-side query timeout.
- A PreToolUse hook escalates destructive `docker rm`/`prune` commands
  aimed at the managed container to an explicit confirmation.
- `sync_graph.py verify` proves the graph matches the source table
  (counts + random spot-check); dedupe marks are reviewable and undoable.
- The Neo4j password lives in a `.neo4j.env` file in your project, which the
  tooling auto-appends to your `.gitignore`.

## Repo layout

Component conventions between the two skills (env-file format, container
naming, dimension-hub shape, schema fingerprint) are documented in
[`references/graph-contract.md`](references/graph-contract.md). This repo is
also its own single-plugin marketplace (`.claude-plugin/marketplace.json`).

## License

TBD.

---
name: sql-to-neo4j
description: >-
  End-to-end pipeline that takes a table in a local self-hosted PostgreSQL
  (Docker) database and turns it into a browsable Neo4j graph. Use this whenever
  the user wants to move, migrate, mirror, load, import, or "graph" a SQL/Postgres
  table into Neo4j, build a knowledge graph from relational data, visualize table
  relationships as a graph, or asks something like "put my orders table into
  Neo4j", "graph this Postgres table", "turn this SQL data into a graph I can
  explore", or "spin up Neo4j and load X into it". The skill locates the table,
  auto-infers a node/relationship model, brings up a local Neo4j Community Docker
  container (reusing an existing one or pulling+running a new one), streams the
  rows in with constraints and indexes, and hands back a Neo4j Browser URL with a
  visualization query pre-loaded. Trigger it even if the user only names one half
  (e.g. "I have Postgres in Docker, I want a graph database") — this is the bridge.
allowed-tools: >-
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/pg_inspect.py *),
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/neo4j_manager.py *),
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/load_graph.py *),
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/sync_graph.py *),
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/graph_snapshot.py *)
---

# SQL table → Neo4j graph (end to end)

This skill carries a single PostgreSQL table all the way to a live, explorable
Neo4j graph and returns a URL the user can click. It is built around five small,
composable scripts bundled with this skill (referenced below via
`${CLAUDE_SKILL_DIR}/scripts/`). Run them in order; each writes an artifact the
next one reads, so the user (or you) can inspect and hand-tune the plan in the
middle. Working artifacts (`model.json`, `.neo4j.env`) are written to the
current project directory — the shared conventions between this skill and the
cypher-translator skill are documented in
`${CLAUDE_PLUGIN_ROOT}/references/graph-contract.md`.

The whole point is that a flat table is boring to look at, but the same data as a
graph — orders fanning into shared customers, products, and statuses — is
immediately legible. So the modeling step matters as much as the plumbing.

## Prerequisites (check, don't assume)

- Docker daemon reachable (`docker info`). If it isn't running, tell the user and
  stop — this skill needs it for both databases.
- `uv` on PATH (https://docs.astral.sh/uv/). The scripts are self-contained:
  each declares its own dependencies inline (PEP 723), and `uv run` provisions
  an isolated environment on first use — no `pip install`, no project
  `pyproject.toml` changes, works in any repository.
- The Postgres connection string (DSN) and the target table name. If the user
  hasn't given a DSN, ask, or discover it: a Postgres container's mapped port
  shows in `docker ps`, and the user usually knows the db/user/password they set.

> **DB safety rule:** point the DSN only at a **local** Postgres (a Docker
> container or local copy), never at a shared/production database. The scripts
> only read, but `pg_inspect.py` runs a `COUNT(DISTINCT)` per text column —
> heavy scan load that belongs on a local copy. If the project's own notes
> (CLAUDE.md) name specific safe local instances, prefer those.

## The connection string

A DSN looks like `postgresql://USER:PASSWORD@HOST:PORT/DBNAME`. For a local
Dockerized Postgres it's typically `postgresql://postgres:PASSWORD@localhost:5432/DBNAME`.
Keep it in a shell variable so it never gets echoed into a document:

```bash
DSN="postgresql://postgres:PASSWORD@localhost:5432/mydb"
```

## Step 1 — Locate the table and infer the graph model

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/pg_inspect.py --dsn "$DSN" --table public.orders --out model.json
```

This connects, confirms the table exists (if not, it says so — list tables with
`psql "$DSN" -c "\dt"` and ask the user which one), and writes `model.json`: the
inferred graph. The inference logic, in plain terms:

- **Each row becomes a node** whose label is the singularized table name
  (`orders` → `:Order`). Its identity is the table's primary key; if the table has
  no PK, the loader hashes the whole row into a stable `_row_key` so re-runs don't
  duplicate.
- **Foreign keys become real relationships** to nodes in the referenced table
  (`orders.customer_id` → `(:Order)-[:HAS_CUSTOMER]->(:Customer)`).
- **Low-cardinality text columns become dimension nodes** — a `status`,
  `category`, or `type` column with only a handful of distinct values turns into a
  shared hub (`(:Order)-[:HAS_STATUS]->(:Status {value:"shipped"})`). This is what
  makes the result look like a graph instead of a star of disconnected rows.
- **Everything else stays as a property** on the row node.

Two special shapes are detected automatically: a **join table** (2-column PK
made of two FKs, e.g. `order_items`) becomes *relationships with properties*
instead of nodes (`mode: "join_table"` in the model), and a
**self-referencing FK** (`manager_id`) is flagged as a hierarchy.

`model.json` is deliberately readable. Show the user the summary the script
prints (labels + relationships) and offer to adjust before loading. **Propose
semantic names**: the generated names are mechanical (`HAS_CUSTOMER`); suggest
1–3 renames that read like sentences (`PLACED_BY`, `REPORTS_TO`,
`CONTAINS`) based on what the table actually means, show the before/after,
and apply only what the user approves by editing `model.json` — renames only
change the name of an existing entry; never add entities or relationships
that don't exist in the model. They can also change labels, drop a
spurious dimension, tune `--max-distinct-abs` / `--max-distinct-ratio`, or
hand-tune visually: the script also writes `model.arrows.json`, importable at
https://arrows.app. See `${CLAUDE_SKILL_DIR}/references/model-format.md` for
the exact fields.

## Step 2 — Bring up Neo4j (reuse if it's already there)

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/neo4j_manager.py up --env-file .neo4j.env
```

This follows the "don't rebuild what exists" ladder the user asked for:

1. A container named `graph-neo4j` already **running** → reuse it.
2. A **stopped** container of that name exists → `docker start` it.
3. The `neo4j:5-community` **image is already pulled** → run a fresh container.
4. Otherwise → pull the image, then run.

(For the default Community engine there is no image to *compile* — "build" means
pull the official image and run it configured. If the project needs a custom image
with plugins like APOC baked in, that's a project Dockerfile; see
`${CLAUDE_SKILL_DIR}/references/neo4j-docker.md`.)

On success it waits until Neo4j actually answers on its HTTP port, then writes
`.neo4j.env` (URI, HTTP URL, user, password, container name) and appends it to
`.gitignore` so the password doesn't get committed. Both the loader and the user
read connection details from this one file. Flags: `--name`, `--password`,
`--http-port`, `--bolt-port`, `--tag`. `status` and `down` subcommands exist too.

## Step 3 — Transfer, index, and get the visualization URL

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/load_graph.py --model model.json --dsn "$DSN" --env-file .neo4j.env
```

This is idempotent — nodes are `MERGE`-d on their keys, so you can tune the model
and reload without wiping. It:

1. Creates a **uniqueness constraint on every node key** first. In Neo4j a unique
   constraint is also an index, so this guarantees identity *and* stops each
   per-row `MERGE` from doing a full scan — without it, loading a large table
   crawls. This is the indexing the user asked for.
2. Streams rows from Postgres in batches (server-side cursor) and loads each batch
   with a single `UNWIND`, which is far faster than row-by-row.
3. Prints node/relationship counts, the **visualization query**, a **Neo4j
   Browser URL** (connection form pre-filled via `dbms`/`db` params, query
   pre-loaded), and up to four **starter queries** shaped from the actual
   model, each annotated with its SQL equivalent — also available in the
   `===RESULT_JSON===` line under `starter_queries`.

Pass `--wipe-label` to clear existing nodes of the primary label before loading
(a clean re-import; node-table mode only — join-table models MERGE on their
endpoints and update in place). `--batch-size` tunes throughput.

## Step 4 — Hand it back to the user

Relay the Browser URL from the loader's output. It opens Neo4j Browser with
the connection form pre-filled (host + user) and a query like
`MATCH p=(n:Order)-[r]->(m) RETURN p LIMIT 300` already in the editor — the
user only types the password (it lives in `.neo4j.env`) and presses ▶ to see
the graph. The loader also prints a `===RESULT_JSON===` line with the URL,
counts, Bolt address, and `starter_queries`; use it to give the user a tidy
summary.

Good closing message: the table that was loaded, how many nodes of each label
and how many relationships were created, the clickable Browser URL, the login
user — and **relay 2–3 of the starter queries** (title + Cypher + the SQL
equivalent line) as "try these next". They're generated from the user's own
model, so they always work, and the SQL annotations are what make the graph
click for SQL-fluent users. If they want richer dashboards than Browser,
point them at `${CLAUDE_SKILL_DIR}/references/neo4j-docker.md`. And for
users who don't want to open Neo4j Browser at all (or want to email the
graph to someone), offer the **offline snapshot**:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/graph_snapshot.py --out graph-snapshot.html --title "<table> graph"
```

One self-contained HTML file — embedded data, vanilla-JS force layout, no
CDN, no login, opens anywhere. Caps at 400 highest-degree nodes by default
(`--max-nodes`; the header says "sampled" when capped).

## Keeping it fresh (sync & verify)

When the source table changes later, don't re-explain the pipeline — sync:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/sync_graph.py run --model model.json --dsn "$DSN" [--updated-column updated_at]
uv run ${CLAUDE_SKILL_DIR}/scripts/sync_graph.py verify --model model.json --dsn "$DSN"
```

`run` re-MERGEs idempotently (incremental when an `--updated-column` and a
stored high-water mark exist — state in `.neo4j-sync-state.json`); `verify`
proves consistency (counts + random PK spot-check, exit 3 on mismatch) —
offer it after any sync so the user gets receipts. Deletes are never
propagated: for those, a clean rebuild via `load_graph.py --wipe-label`.

## When something goes wrong

- **`docker info` fails** → daemon down; ask the user to start Docker.
- **Table not found** → run `psql "$DSN" -c "\dt"`, show the list, confirm the name
  (remember schema qualification, e.g. `sales.orders`).
- **Neo4j never becomes healthy** → `docker logs graph-neo4j`; usually too
  little memory. (Busy default ports are auto-avoided at creation since
  v0.2 — a fresh container picks the next free pair and says so; explicitly
  requested ports fail fast instead.)
- **Referenced-table nodes look empty** → expected. Loading only the `orders`
  table creates *stub* `:Customer` nodes carrying just the FK value. If the user
  wants those fleshed out, run the skill again on the referenced table (e.g.
  `customers`); the `MERGE` will enrich the same nodes in place.
- **`CALL (n, row)` syntax errors during load** → the loader's Cypher uses the
  Neo4j 5.23+ scoped-`CALL` form. A fresh `neo4j:5-community` pull is new enough;
  a *reused* older container isn't — `neo4j_manager.py down`, then
  `docker rm <name>`, then `up` again with a current image.
- **Auth errors on reload** → the password in `.neo4j.env` must match the container;
  if they diverged, `neo4j_manager.py down` + `up` with an explicit `--password`,
  or reuse the existing one.

## Reference files

- `${CLAUDE_SKILL_DIR}/references/model-format.md` — the `model.json` schema and how to hand-tune it.
- `${CLAUDE_SKILL_DIR}/references/neo4j-docker.md` — container internals, custom images (APOC/GDS),
  ports, volumes, and richer visualization options beyond Browser.

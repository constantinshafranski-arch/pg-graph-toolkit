# The graph contract

The two skills in this plugin cooperate through a small set of on-disk and
in-graph conventions. This file is the single source of truth for that
contract. **Every statement here is derived from the current script code** —
if a script changes one of these behaviors, update this file in the same
commit.

## Artifacts (all written to the *project* directory, never the plugin)

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `model.json` | `sql-to-neo4j/scripts/pg_inspect.py` | `load_graph.py` (and the user, for hand-tuning) | The inferred graph model. Format: `sql-to-neo4j/references/model-format.md` |
| `.neo4j.env` | `neo4j_manager.py up` (also auto-recovered by `graph_context.py` from `docker inspect` if lost) | `load_graph.py`, `graph_context.py`, `run_cypher.py`, the user | Connection details + password. `neo4j_manager.py` appends it to the project `.gitignore` |
| `.neo4j-translator-memory.json` | the cypher-translator skill (via the Write tool, per its SKILL.md) | the cypher-translator skill | Conversation memory: last ≤20 turns (`ts`, `request`, `cypher`, `row_count`, `result_summary`, `entities`) + the `graph_fingerprint` they were valid for |
| `model.arrows.json` (or `<out>.arrows.json`) | `pg_inspect.py` | the user (Arrows.app import) | Visual model editor artifact; derived, never read back |
| `.neo4j-sync-state.json` | `sync_graph.py run` (only when `--updated-column` is used) | `sync_graph.py` | Per-table incremental high-water marks |
| `graph-snapshot.html` (or `--out`) | `graph_snapshot.py` | the user (any browser, offline) | Self-contained no-Neo4j graph view; derived, capped at `--max-nodes` highest-degree nodes, never read back |

## `.neo4j.env` format

Plain `KEY=value` lines, no quoting, `#` comments ignored. Keys (exactly these,
written in this order by `neo4j_manager.py`):

```
NEO4J_URI=bolt://localhost:<bolt_port>
NEO4J_HTTP=http://localhost:<http_port>
NEO4J_USER=<user>
NEO4J_PASSWORD=<password>
NEO4J_CONTAINER=<container name>
```

`graph_context.py` writes the same keys (omitting any it could not recover).
Only `neo4j_manager.py` and `graph_context.py` may write this file; nothing
else edits it by hand.

## Container conventions

- Default container name: **`graph-neo4j`** (`--name` overrides; the effective
  name is persisted as `NEO4J_CONTAINER` and preferred by `graph_context.py`).
- Containers created by `neo4j_manager.py` (v0.2+) carry the Docker label
  **`managed-by=pg-graph-toolkit`**. `down` refuses to stop a container
  labeled by another tool (without `--force`); unlabeled containers of the
  exact configured name (pre-v0.2 or external) are stopped with a printed
  note — back-compat for existing users.
- If the default ports are busy at creation time, `up` auto-advances to the
  next free http/bolt pair (up to +10) and prints the choice; explicitly
  requested busy ports fail fast instead. The chosen ports land in
  `.neo4j.env` as usual.
- Image: `neo4j:5-community` by default (`--tag` overrides). The loader's
  Cypher uses the Neo4j 5.23+ scoped-`CALL (n, row)` form — a reused older
  container will fail; recreate it with a current image.
- Default ports: HTTP 7474, Bolt 7687 (`--http-port` / `--bolt-port` override).
- Default user `neo4j`. Data lives in a named Docker volume `<name>-data`;
  `neo4j_manager.py down` stops the container but never removes the volume.
- New containers get `NEO4J_PLUGINS=["apoc"]` by default; `up --with-gds`
  adds the free Graph Data Science Community library. Plugins are baked in
  at container creation only — an existing container keeps what it started
  with (recreate to change).
- Container lifecycle (create, password, ports) belongs to **sql-to-neo4j**.
  The translator side only ever does `docker start` on a stopped container.

## Dimension-hub convention

`pg_inspect.py` promotes low-cardinality text columns to "dimension" nodes:

- Hub node shape: `(:<PascalSingular(column)> {value: <cell value>})`, i.e. the
  hub's key property is always **`value`** (`to_key_prop: "value"` in
  `model.json`).
- Rows link to hubs as `(:Row)-[:HAS_<COLUMN>]->(:Hub)`.
- Promotion thresholds in `pg_inspect.py`: distinct > 1, distinct <
  row count, and distinct ≤ `min(--max-distinct-abs [50], max(20, rows ×
  --max-distinct-ratio [0.1]))`. FK and PK columns are never dimensions.
- On the translator side, `graph_context.py` treats any label with ≤ 50 nodes
  (`DIMENSION_MAX_NODES`) as a dimension and reports its `value` properties as
  `dimension_values` in the digest. **These two 50s are the same convention**;
  change them together.

## Model modes (v0.3+)

`model.json` carries `"mode": "node_table"` (rows → nodes; FK id columns are
folded into relationships and no longer duplicated as node properties) or
`"mode": "join_table"` (a 2-column-PK-of-two-FKs table; rows → relationships
with properties, `join_relationship` replaces `primary_node`; rows with a
NULL endpoint are skipped and counted — only reachable in hand-edited models, since auto-inferred join PKs are NOT NULL). `pg_inspect.py` also writes
`<out>.arrows.json`, an Arrows.app import for visual hand-tuning — a derived
artifact, never read back by any script.

## Digest patterns (v0.3+)

`graph_context.py`'s digest includes `patterns`: directed
`{from, type, to, count}` entries (top 200 by count). They are the ground
truth for relationship direction in generated Cypher, and what the refine
step consults before flipping an arrow.

## Graph fingerprint

`graph_context.py` computes
`sha256("<label>:<count>|<label>:<count>|…")[:16]` over all labels sorted by
name. The translator stores this as `graph_fingerprint` in the memory file and
discards remembered turns when it changes (graph was reloaded). Any change to
the fingerprint algorithm invalidates all existing memory files — bump it only
deliberately.

## Read-only guarantee

`run_cypher.py` opens its session with the driver's `READ_ACCESS` mode, so the
server itself rejects write clauses. Since v0.2 it also EXPLAIN-compiles the
query first (syntax errors return with `"stage": "explain"`, nothing executes)
and applies a server-side transaction timeout (default 30s, `--timeout`).
The translator skill must never bypass the script (no `cypher-shell`, no
ad-hoc driver code) — the script carries the access mode, the pre-flight, the
timeout, the row cap (≤1000), and JSON serialization.

## graph-status skill

Status checks only: it reads `.neo4j.env`, checks container state via
`docker ps`, and may run `graph_context.py` for a contents summary. It issues
no lifecycle commands of its own — though `graph_context.py`, which it
delegates to, will `docker start` a stopped container, so the skill only
invokes it after confirming the container is running. Never prints
`NEO4J_PASSWORD`.

## graph-insights skill

Read-only like graph-status: `graph_health.py` (in the skill's `scripts/`)
runs per-label counts, sampled key shapes, degree/orphan scans (skipped above
`--big-label-cap`, default 500k), constraint listing, and deterministic
review findings (`generic_name`, `mixed_key_shapes`, `missing_constraint`,
`supernode_dimension`). Its `DIMENSION_MAX_NODES = 50` mirrors the shared
dimension convention. Narration and SQL analogies live in the SKILL.md, not
the script.

## Sync & verify (v0.4+)

`sync_graph.py` (sql-to-neo4j scripts) reuses `load_graph.py`'s internals.
`verify` is read-only on both sides: count comparison plus a random-PK
spot-check; exit 3 on mismatch. `run` is an idempotent re-MERGE — full, or
incremental (`WHERE col >= high_water`; ties re-merge harmlessly) when
`--updated-column` and a stored mark exist. Deletes are never propagated;
a clean rebuild is `load_graph.py --wipe-label`.

## SAME_AS convention (graph-dedupe, v0.4+)

The ONLY write this plugin ever performs outside loading/syncing is
`SAME_AS` relationships created by `find_duplicates.py`, always carrying
`source: 'pg-graph-toolkit-dedupe'`, `status: 'pending'` (until a human
changes it), `confidence`, `method`, and `property`. The tool never merges
or deletes user data; `--clear` removes exactly its own tagged edges
(directed match). Bands: ≥0.95 auto-merge candidates, 0.85–0.95 review.

## Docker guard hook (v0.4+)

`hooks/hooks.json` runs `scripts/docker_guard.py` (stdlib, fail-open) on
every Bash PreToolUse. It escalates to an "ask" confirmation — never a hard
deny — for `docker ... rm` naming `graph-neo4j*` and for any `docker ...
prune`. Everything else gets no decision (normal permission flow).

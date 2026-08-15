# Changelog

## 0.5.0 — The last mile

Closes the one unshipped promise from the original design's "smoother
hand-off" card: the no-Neo4j view.

- **New `graph_snapshot.py`** (sql-to-neo4j skill): exports the graph as a
  single self-contained HTML file — embedded data, ~150 lines of vanilla
  canvas JS, zero CDN/network/login. Drag, pan, zoom, hover tooltips,
  label legend, degree-sized nodes. Read-only; keeps the highest-degree
  nodes when capping (`--max-nodes`, default 400) and says "sampled" in
  the header when it does. Render-verified headless against the live
  fixture graph (106 nodes / 321 rels, zero JS errors).

## 0.4.0 — Trust & depth

The graph earns your trust: prove it matches the source, find duplicates
without destroying anything, and an extra airbag on Docker. (The optional
Neo4j MCP add-on from the original v0.4 idea list is deferred.)

- **New `graph-dedupe` skill**: precision-first duplicate funnel
  (normalized-exact, then fuzzy within blocks) writing reviewable
  `SAME_AS {status:'pending', confidence, method}` edges — never merges,
  never deletes; `--dry-run` default ritual and `--clear` undo. Verified
  live: planted "Globex LLC"/"Globex L.L.C." caught at 0.99,
  "Jonathan/Jonathon Smith" at 0.93 in the review band.
- **sync + verify** (`sync_graph.py`): `verify` compares counts and
  spot-checks random PKs across Postgres and the graph (exit 3 on
  mismatch — a deliberately punched hole was caught in testing); `run`
  re-MERGEs idempotently, incrementally when `--updated-column` has a
  stored high-water mark. Deletes intentionally not propagated
  (documented).
- **Docker safety hook**: a PreToolUse guard escalates `docker rm` of
  `graph-neo4j*` and any `docker prune` to an explicit user confirmation
  ("ask", never a hard deny; fail-open on parse errors).
- **APOC by default, GDS on request**: new containers start with
  `NEO4J_PLUGINS=["apoc"]`; `up --with-gds` adds the free Graph Data
  Science Community library (creation-time only).

## 0.3.0 — Smarter

Model inference grows up, and the graph learns to explain itself. Every
feature verified against live Postgres + Neo4j fixtures during development.

- **Join-table detection**: a table whose 2-column PK is two FKs becomes
  *relationships with properties* (`mode: "join_table"`), not a pile of
  nodes; the loader gains a matching mode (NULL endpoints skipped and
  counted) plus join-shaped starter queries (busiest target,
  bought-together pairs, sum over a relationship property).
- **Self-FK hierarchies** flagged (`self_reference: true`) and surfaced in
  the inspect summary; verified with variable-length chain queries.
- **FK folding**: FK id columns are no longer duplicated as node properties
  once they become relationships (PK columns always kept).
- **Arrows.app export**: every inspect also writes `<out>.arrows.json` for
  visual hand-tuning at https://arrows.app.
- **Semantic naming step**: the sql-to-neo4j skill now proposes
  sentence-like relationship renames (PLACED_BY, REPORTS_TO) for user
  approval before loading — suggestions only, applied by editing model.json.
- **Directed patterns in the digest**: `graph_context.py` reports
  `{from, type, to, count}` patterns; the translator composes directions
  against them and flips the arrow on empty results before giving up.
- **New `graph-insights` skill**: a read-only health pack (hubs, orphans,
  key-shape collisions, constraint coverage) narrated in plain English with
  SQL analogies. Orphans are mapped back to NULLs in the source table —
  the graph audits your SQL data. Deterministic review findings include
  `mixed_key_shapes` (caught a real collision in fixture testing),
  `missing_constraint`, `supernode_dimension` (yes, our own dimension hubs
  at scale), and `generic_name`.
- **Structured memory**: turns now record `row_count` alongside the query.

## 0.2.0 — Harden + welcome

Safety hardening plus the first beginner-facing features. All behavioral
changes were verified against a live Neo4j 5.26 server and a live Postgres
fixture during development.

- **psycopg 3**: `pg_inspect.py` and `load_graph.py` migrated from
  psycopg2-binary to `psycopg[binary]` (byte-identical model output verified;
  full load pipeline re-run live).
- **Query safety**: `run_cypher.py` now EXPLAIN-compiles every query before
  execution (errors return with `stage: explain`) and applies a server-side
  transaction timeout (default 30s, `--timeout`); errors carry a `stage`
  field.
- **Docker manners**: new containers get a `managed-by=pg-graph-toolkit`
  label; `down` refuses to stop containers labeled by other tools (without
  `--force`) and still stops unlabeled pre-v0.2 containers by exact name;
  busy default ports auto-advance to the next free pair.
- **Hand-off**: the Browser link now pre-fills the connection form
  (`dbms`/`db` params); after loading, up to four model-shaped **starter
  queries** are printed and included in `RESULT_JSON`, each with its SQL
  equivalent (all generated queries execution-tested).
- **New `graph-status` skill**: read-only "is it up / what's loaded / where's
  my Browser link" checks without the full translation flow.
- **SQL↔Cypher phrasebook** (`skills/cypher-translator/references/`): answers
  now include a one-line SQL equivalent; the phrasebook documents the classic
  SQL-ism traps.
- Memory-file writes: SKILL.md now tells Claude to re-read the file before
  overwriting (removes a harmless first-write error seen in v0.1 testing).

## 0.1.0 — Port

Mechanical port of the two existing skills into one installable plugin. No
logic changes to the scripts.

- Plugin scaffold: `.claude-plugin/plugin.json`, self-pointing marketplace
  (`.claude-plugin/marketplace.json`), this changelog, README.
- Scripts are now self-contained via PEP 723 inline dependency blocks and run
  with `uv run` — no host-project `pyproject.toml` or `graph` extra required.
- SKILL.md invocations use `${CLAUDE_SKILL_DIR}` so bundled scripts resolve
  from any project; `allowed-tools` pre-approves exactly those commands.
- Repo-specific database safety rules generalized ("local Postgres only");
  project-specific instance lists belong in each project's CLAUDE.md.
- Added `references/graph-contract.md` — the shared conventions between the
  two skills (env-file format, container naming, dimension hubs, fingerprint),
  documented in one versioned place.

Deliberately deferred at the time — all since delivered in 0.2.0.

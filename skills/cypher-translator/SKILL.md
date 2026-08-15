---
name: cypher-translator
description: Use when the user wants to query, explore, or ask questions about the local Neo4j graph (loaded by the sql-to-neo4j skill) in plain English — "show me X in the graph", "translate this to Cypher", "what's in my neo4j", follow-up questions like "of those, only the top 3" — or wants a Cypher query explained or suggested against the actual loaded data.
allowed-tools: >-
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/graph_context.py *),
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/run_cypher.py *)
---

# cypher-translator — English ↔ Cypher against the live local graph

Translates plain-English requests into Cypher grounded in LIVE introspection of
the local Neo4j container, runs them read-only, and keeps a JSON memory of the
conversation so follow-ups ("of those…", "same but per seller") resolve against
prior turns. You (the running model) are the translator; the scripts only
provide ground truth and execution.

All commands run from the project root (the memory file and `.neo4j.env` live
there). Shared conventions with the sql-to-neo4j skill (env-file format,
container name, dimension-hub shape, fingerprint) are documented in
`${CLAUDE_PLUGIN_ROOT}/references/graph-contract.md`.

## Flow (every invocation)

1. **Ground truth first:**
   ```bash
   uv run ${CLAUDE_SKILL_DIR}/scripts/graph_context.py --env-file .neo4j.env
   ```
   Emits the live digest: every label with count + property keys, dimension
   values for small hub labels, relationship types with counts, directed
   `patterns` (`from`-[`type`]->`to` with counts — the ground truth for
   arrow DIRECTION; always compose against these, never guess), a
   `graph_fingerprint`, and connection details. It auto-recovers `.neo4j.env`
   from the container and `docker start`s it if stopped. Exit 2 = no container:
   tell the user to load a graph with the sql-to-neo4j skill, stop.

2. **Read memory** `.neo4j-translator-memory.json` (project root; may not
   exist). If its `graph_fingerprint` differs from the digest's, the graph was
   reloaded — discard `turns`, say so in one line, and continue fresh. A
   missing or unreadable (invalid-JSON) file is the same case: treat as absent
   and start fresh.

3. **Translate.** Compose ONE read-only Cypher query using ONLY labels,
   relationship types, and properties present in the digest. Resolve
   contextual references ("those", "the previous ones") from memory `turns`.
   The graph may hold several loads (several row-node labels); if the request
   is ambiguous about which, pick the best fit, and name your pick in the answer.
   Default `LIMIT 50` unless the user clearly wants everything or an aggregate.
   If the input is already Cypher, explain it against the digest instead
   (plain words, flag anything referencing labels/properties that don't exist).

4. **Execute:**
   ```bash
   uv run ${CLAUDE_SKILL_DIR}/scripts/run_cypher.py --cypher '<query>'
   ```
   Read-only is server-enforced (READ access mode) — write clauses fail with
   `Neo.ClientError.Statement.AccessMode`; never work around that. The script
   also EXPLAIN-compiles the query first (syntax errors return with
   `"stage": "explain"` before anything executes) and applies a server-side
   transaction timeout (default 30s; `--timeout` to adjust — on a timeout
   error, narrow the query rather than raising the limit). On error or
   an empty result that contradicts the digest counts, refine ONCE — first
   suspect: arrow direction (check the digest's `patterns`; if the pattern
   exists in the opposite direction, flip the arrow) — then re-check
   labels/properties, then report honestly.

5. **Answer:** the Cypher, a compact result table, one-line interpretation
   (name which load/label you queried), and 2–3 follow-up suggestions grounded
   in the digest and this result. For SQL-fluent users (the default
   assumption), add a one-line **SQL equivalent** of the query ("in SQL this
   would be: SELECT status, COUNT(*) ... GROUP BY status") and, for any
   non-obvious pattern, the read-it-aloud reading of the arrow ("Order,
   placed by Customer"). Mappings and classic SQL-ism traps:
   `${CLAUDE_SKILL_DIR}/references/sql-cypher-phrasebook.md` — consult it
   before translating anything beyond a trivial MATCH.

6. **Persist memory** (Write tool). Create the file if missing; otherwise
   append to the existing `turns`. If the file exists, make sure you've Read
   it in this conversation before overwriting (the Write tool requires it —
   re-read right before writing if in doubt). Keep only the last 20 turns, and always
   store the CURRENT digest's `graph_fingerprint` (so the next invocation
   compares against what this one actually saw):
   ```json
   {
     "graph_fingerprint": "<digest value>",
     "turns": [
       {"ts": "<date -Iseconds>", "request": "<user's words>",
        "cypher": "<query run>", "row_count": <rows returned>,
        "result_summary": "<1 line: rows, notable values>",
        "entities": ["<labels/rel types touched>"]}
     ]
   }
   ```

## Rules

- **Live digest is the only schema source.** Never read `model.json` or any
  other saved load artifact to learn the schema — they go stale the moment
  the graph is reloaded.
- **Never bypass the scripts** to query (no `cypher-shell`, no ad-hoc driver
  code): the scripts carry the read-only mode, row caps, and serialization.
- **Never edit `.neo4j.env`** by hand; `graph_context.py` owns it.
- Container lifecycle beyond `docker start` (create, password, ports) belongs
  to the sql-to-neo4j skill — don't duplicate it here.
- `dimension_values` in the digest are the hub label's `value` property (the
  sql-to-neo4j loader's convention for dimension hubs).
- Project knowledge from outside the digest (docs, CLAUDE.md, code) may be
  offered as interpretation, clearly labeled as such — never as schema, and
  never inside the Cypher unless the digest confirms the names.

## Common mistakes

| Mistake | Fix |
|---|---|
| Inventing a `status`-like property that isn't in the digest | Dimension hubs are separate labels (e.g. `(:Row)-[:HAS_X]->(:X {value})`); check `dimension_values` |
| Treating memory as schema | Memory is conversation context only; schema always comes from step 1 |
| Retrying a failed write query with `MERGE`/`SET` variants | This skill is read-only by contract; say so |
| Skipping memory write because "answer already delivered" | Step 6 is what makes the next invocation contextual — always write it |

---
name: graph-insights
description: Use after a graph is loaded, or on request, to narrate what's interesting or wrong in the local Neo4j graph — "give me insights on my graph", "graph health check", "review my graph model", "is my model any good?", "anything weird in the graph?", "find data quality issues". Runs a read-only health pack (hubs, orphans, degree skew, constraints, model anti-patterns) and tells the story in plain English with SQL analogies. Not for answering data questions (use cypher-translator) or plain status (use graph-status).
allowed-tools: >-
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/graph_health.py *)
---

# graph-insights — narrate the graph's health, find the story

Runs a deterministic, read-only health pack and turns the numbers into a
short narrated report. The audience knows SQL well and graphs barely —
explain every finding with a SQL analogy where one exists.

## Flow

1. **Collect** (assumes the graph is up; if unsure, the cypher-translator's
   `graph_context.py` flow starts it):
   ```bash
   uv run ${CLAUDE_SKILL_DIR}/scripts/graph_health.py --env-file .neo4j.env
   ```
   JSON out: per-label counts + sampled `key_shapes` + `top_degrees` +
   `orphans`, total relationships, `constraints`, and deterministic `review`
   findings.

2. **Narrate** — a 4–6 bullet story, most interesting first. For each
   insight include the click-to-run Cypher that would show it (the insight
   doubles as a Cypher lesson). Patterns to look for:
   - **Hubs**: "your biggest hub is X with N connections" (top_degrees).
   - **Orphans**: nodes with zero relationships. In graphs made by this
     plugin, an orphaned row node almost always means a NULL FK / dimension
     value **in the source table** — say that, and give the SQL to find the
     source rows (`SELECT * FROM t WHERE fk_col IS NULL`). The graph just
     audited their SQL data — call that out.
   - **Concentration**: if a top-5 hub holds a large share of a label's
     relationships, mention the 80/20 shape.
   - **Empty results are findings too**: zero orphans = "every row connected
     cleanly" — worth a bullet.

3. **Review findings** (the `review` array) — translate each `kind` into a
   SQL-flavored explanation:
   - `missing_constraint` → "like a table with no index on its key: every
     MERGE full-scans. Fix: `CREATE CONSTRAINT ... REQUIRE n.key IS UNIQUE`."
   - `mixed_key_shapes` → "two different loads collided on one label name —
     like two tables UNIONed by accident. Consider reloading one side with a
     different label (edit `to_label`/`label` in its model.json)."
   - `supernode_dimension` → "a status-style hub with 100k+ edges funnels
     every query through one node — at that scale prefer a label (`:Active`)
     over a hub. (This plugin's own dimension hubs hit this limit — say so
     honestly.)"
   - `generic_name` → suggest a concrete rename.
   If `review` is empty, grade the model as healthy in one line — don't
   invent problems.

4. **Close** with 2–3 suggested next questions for the cypher-translator,
   grounded in what the health pack actually showed.

## Rules

- Read-only, always — this skill never fixes anything itself; it proposes
  the fix (Cypher or model.json edit) and lets the user decide.
- Never print `NEO4J_PASSWORD`.
- Don't re-derive schema from memory or model files — everything comes from
  the health pack's live output (plus the digest if the translator ran).
- Large labels may report `degree_scan: skipped` — say so rather than
  guessing (`--big-label-cap` raises the limit when the user wants the full
  scan).

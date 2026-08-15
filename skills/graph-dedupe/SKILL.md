---
name: graph-dedupe
description: Use when the user suspects duplicate entities in the local Neo4j graph — "find duplicates in my customers", "are ACME Corp and Acme Corporation the same?", "dedupe the graph", "clean up duplicate nodes" — or after graph-insights surfaced suspicious near-identical values. Finds likely duplicates with a precision-first funnel and marks them with reviewable SAME_AS edges. It NEVER merges or deletes anything.
allowed-tools: >-
  Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/find_duplicates.py *)
---

# graph-dedupe — find likely duplicates, gently

Duplicates quietly break the connection-hopping that makes graphs useful
("ACME Corp" and "Acme Corporation" split one customer's history in two).
This skill finds them and marks them for review. **Nothing is ever merged or
destroyed automatically** — the only write is `SAME_AS` edges tagged
`source: 'pg-graph-toolkit-dedupe'` with `status: 'pending'`.

## Flow

1. **Pick the target.** Ask (or infer from context) which label and which
   text property to scan — usually a name-like property. The translator's
   digest shows what exists; person/company/product names are the classic
   candidates. Ids and numeric columns are pointless targets — decline those.

2. **Dry-run first, always:**
   ```bash
   uv run ${CLAUDE_SKILL_DIR}/scripts/find_duplicates.py --label Customer --property name --dry-run
   ```
   The JSON gives `auto_merge_candidates` (confidence ≥ 0.95),
   `needs_review` (0.85–0.95), values included.

3. **Adjudicate the borderline band yourself.** You can see both values —
   apply judgment ("Jon Smith"/"John Smith" in the same city: likely same;
   "Product 1"/"Product 4": clearly different despite the fuzzy score) and
   present your call per pair, clearly labeled as a suggestion. Precision
   first: when unsure, say "probably distinct" — a false merge costs far
   more than a missed one.

4. **With the user's go-ahead, write the marks** (same command without
   `--dry-run`). Relay the `review_queue_cypher` from the output — the
   review queue is just a query result, SELECT-able like anything else.

5. **Acting on approved pairs is the user's move, not yours.** If they want
   two nodes actually combined, give them the Cypher to run themselves in
   Browser and explain what it does, e.g.:
   ```cypher
   MATCH (a)-[r:SAME_AS {status:'pending'}]->(b)
   WHERE elementId(a) = '<id>' AND elementId(b) = '<id>'
   SET r.status = 'confirmed'
   ```
   (Even "confirmed" only records the decision — physically merging nodes is
   deliberately out of scope for this version; recommend keeping the
   canonical node + SAME_AS edges, which most queries can follow.)

6. **Undo is built in:** `--clear` removes every edge this tool wrote for
   the label, and nothing else.

## Rules

- Never run with `--dry-run` omitted before the user has seen the dry run.
- Never DETACH DELETE, never merge properties, never touch non-SAME_AS
  relationships — if asked to, explain the gentle-mode contract and offer
  the manual Cypher instead.
- Never print `NEO4J_PASSWORD`.
- Large labels: the scan caps at `--limit` (default 5000) nodes — say so
  when the label is bigger.

---
name: graph-status
description: Use for quick status checks on the local Neo4j graph — "is neo4j up?", "what's loaded in the graph?", "graph status", "where's my browser link?", "how many nodes do I have?". Answers from the container state and a fast schema digest without invoking the full translation flow. For actual data questions ("show me X", "which customers…"), use the cypher-translator skill instead.
allowed-tools: >-
  Bash(docker ps *)
---

# graph-status — is it up, what's in it, where do I look

A lightweight status check. Do NOT translate questions or run data queries
here — that's the cypher-translator skill's job.

## Flow

1. **Read `.neo4j.env`** in the project root (Read tool; it may not exist).
   It has `NEO4J_CONTAINER`, `NEO4J_HTTP`, `NEO4J_URI`, `NEO4J_USER`.
2. **Container state** (default name `graph-neo4j` if no env file):
   ```bash
   docker ps -a --filter name=^graph-neo4j$ --format '{{.State}} {{.Status}} {{.Ports}}'
   ```
   No output → no container: say so and point at the sql-to-neo4j skill. Not
   running → mention it can be started by asking a graph question (the
   translator auto-starts it) or via the sql-to-neo4j skill.
3. **Contents summary** (only if the container is running and the user asked
   what's loaded): run the translator's digest script for live labels, counts
   and relationship types:
   ```bash
   uv run ${CLAUDE_PLUGIN_ROOT}/skills/cypher-translator/scripts/graph_context.py --env-file .neo4j.env
   ```
   (This may prompt for permission — it's the cypher-translator skill's
   script, pre-approved only there. Note it will also `docker start` a
   stopped container, which is why step 2's running check comes first.)
   Summarize: labels with counts, relationship types, and which labels look
   like separate loads.
4. **Answer compactly**: container state, node/relationship totals per label,
   the Browser URL (`NEO4J_HTTP` + `/browser/`), and the login user — password
   is in `.neo4j.env`, don't print it.

## Rules

- Never print `NEO4J_PASSWORD`.
- No lifecycle commands of your own (no docker start/stop/rm); the only
  exception is indirect — the digest script may start a stopped container,
  so only invoke it when step 2 showed the container running.
- If both the env file and the container are missing, the answer is simply
  "nothing is loaded yet" plus the one-line pointer to sql-to-neo4j.

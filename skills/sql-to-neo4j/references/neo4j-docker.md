# Neo4j in Docker — internals, custom images, and visualization

`neo4j_manager.py up` runs the official **`neo4j:5-community`** image with sensible
defaults. This file covers what it does under the hood and how to go further.

## What the default `up` does

```bash
docker run -d --name graph-neo4j \
  --label managed-by=pg-graph-toolkit \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/<password> \
  -e NEO4J_server_memory_heap_max__size=1G \
  -e NEO4J_PLUGINS='["apoc"]' \
  -v graph-neo4j-data:/data \
  neo4j:5-community
```

- **7474** = HTTP / Neo4j Browser (the URL handed back to the user).
- **7687** = Bolt (what the Python driver and `cypher-shell` connect to).
- Data lives in the named volume `graph-neo4j-data`, so `down` (stop) and a later
  `up` (start) preserve the graph. To throw the data away entirely:
  `docker rm -f graph-neo4j && docker volume rm graph-neo4j-data`.

The reuse ladder means calling `up` repeatedly is safe and cheap: running → reuse,
stopped → start, image present → run, else pull → run.

## Reusing an engine another project already configured

If the project already stood up Neo4j under a different container name, point the
skill at it: `neo4j_manager.py up --name <their-name>`. The manager will detect it,
reuse it, and write `.neo4j.env` from the container's own inspected config
(ports + `NEO4J_AUTH`), so the loader talks to the right instance.

## Plugins (APOC, GDS)

Since v0.4 `neo4j_manager.py up` sets `NEO4J_PLUGINS='["apoc"]'` on new
containers (the official image fetches the jar at first start — one-time
network access; `--no-plugins` skips it for offline machines), and
`--with-gds` adds the free Graph Data Science Community library. No
Dockerfile needed for those. A custom *image* is only worth it when you want
plugin jars baked in for fully offline container creation:

```dockerfile
FROM neo4j:5-community
ENV NEO4J_PLUGINS='["apoc"]'
# GDS is Enterprise-gated for some features; APOC Core ships free.
```

Build and run it, then load as normal:

```bash
docker build -t graph-neo4j-apoc .
docker run -d --name graph-neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/<password> -v graph-neo4j-data:/data graph-neo4j-apoc
uv run <skill dir>/scripts/neo4j_manager.py up   # detects the running container and reuses it
# (<skill dir> = this skill's directory; in SKILL.md commands it is ${CLAUDE_SKILL_DIR})
```

## Visualization beyond the pre-loaded query

The skill hands back a Neo4j Browser URL with a first query already in the editor.
Browser is enough for exploring — it renders nodes/relationships with force layout,
lets you expand neighbors by double-clicking, and style by label/property.

Nicer starting queries to suggest to the user once they're in:

```cypher
// The whole neighborhood of the busiest hub
MATCH (n:Order)-[r]->(m) RETURN n, r, m LIMIT 300;

// Group rows by a dimension to see the shape of the data
MATCH (s:Status)<-[:HAS_STATUS]-(o:Order) RETURN s.value, count(o) ORDER BY count(o) DESC;

// Two-hop: customers who share a product category
MATCH (c:Customer)<-[:HAS_CUSTOMER]-(:Order)-[:HAS_CATEGORY]->(cat:Category)
RETURN c.id, collect(DISTINCT cat.value);
```

If the user wants dashboards rather than ad-hoc exploration, **NeoDash** runs as a
separate container (`neo4j-labs/neodash`) pointed at the same Bolt URL; **Bloom**
is richer but Enterprise-only. Browser is the zero-setup default the skill targets.

## cypher-shell (no browser needed)

To run Cypher from the terminal against the same container:

```bash
docker exec -it graph-neo4j cypher-shell -u neo4j -p <password> \
  "MATCH (n) RETURN labels(n)[0] AS label, count(*) ORDER BY count(*) DESC"
```

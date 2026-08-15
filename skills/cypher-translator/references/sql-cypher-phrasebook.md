# SQL ↔ Cypher phrasebook

For answering users who think in SQL (most of them). Use these mappings when
annotating a query with its SQL equivalent, and to catch yourself importing
SQL-isms into Cypher.

## Core mappings

| You'd write in SQL | In Cypher | Note |
|---|---|---|
| `SELECT * FROM orders` | `MATCH (o:Order) RETURN o` | label ≈ table, node ≈ row |
| `WHERE status = 'shipped'` | `WHERE s.value = 'shipped'` after `-[:HAS_STATUS]->(s:Status)` | in this plugin's graphs, categorical columns are hub nodes, not properties |
| `JOIN customers c ON o.customer_id = c.id` | `(o:Order)-[:HAS_CUSTOMER]->(c:Customer)` | the join is stored — read the arrow like a sentence |
| `GROUP BY x` + aggregate | just aggregate: `RETURN d.value, count(o)` | Cypher groups implicitly by every non-aggregated RETURN item |
| `SELECT ... ORDER BY n DESC LIMIT 10` | `ORDER BY n DESC LIMIT 10` | same order as SQL — this one transfers directly |
| self-JOIN (two aliases of one table) | `(a:Order)-->(x)<--(b:Order) WHERE elementId(a) < elementId(b)` | the elementId inequality removes mirror duplicates |
| `WITH RECURSIVE` hierarchy walk | `-[:REPORTS_TO*1..]->` | variable-length pattern; add an upper bound like `*1..6` |
| `IN (a, b, c)` | `IN ['a', 'b', 'c']` | brackets, not parens |
| `COUNT(DISTINCT x)` | `count(DISTINCT x)` | same idea |
| `HAVING count(*) > 5` | `WITH d, count(o) AS c WHERE c > 5` | WITH is Cypher's pipeline step — filter after aggregating |
| `MAX(x)` to find "the top row" | `ORDER BY x DESC LIMIT 1` | returning the whole row via max() is a classic SQL-ism trap |

## Classic SQL-ism traps (check before every query)

- **No GROUP BY keyword exists.** Writing it is a syntax error; grouping is
  implicit.
- **Direction matters.** `(a)-[:R]->(b)` and `(a)<-[:R]-(b)` are different
  statements; check the digest's relationship list for the real direction.
- **Property access needs the node variable** (`o.amount`, never bare
  `amount`).
- **NULL semantics**: missing property comparisons are NULL-ish like SQL —
  `WHERE o.x IS NOT NULL` exists and works.
- **Dimension values live on the hub** (`s.value`), not on the row node — the
  most common wrong-WHERE in this plugin's graphs.

## Explaining a query back ("read it aloud" rule)

`(o:Order)-[:PLACED_BY]->(c:Customer)` reads left to right as a sentence:
"an Order, placed by a Customer." Offer that reading whenever showing Cypher
to a SQL-fluent user — it's the fastest way arrows start making sense.

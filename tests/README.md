# Live verification harness

There is no mocked test suite — the scripts are verified against a real
PostgreSQL and a real Neo4j (Community, 5.26+). `fixture.sql` creates the
tables used during development; expected outcomes:

| Table | Expected inference |
|---|---|
| `orders` | node_table: `(:Order)` keyed on id; HAS_CUSTOMER FK rel; Status + Product dimension hubs; `customer_id` folded out of properties |
| `order_items` | **join_table**: `(:Order)-[:ORDER_ITEM {qty, unit_price}]->(:Product)`; 100 rows → 100 relationships |
| `employees` | node_table with self-referencing HAS_MANAGER (hierarchy); loading it and running a `*1..` chain query resolves depth-3 chains to `CEO`; `Loner` becomes the one orphan graph-insights reports |
| `shipment_details` | node_table (regression: its 2-column PK is ONE composite FK — must NOT become join_table) |

Loading `orders` (dimension hub `:Product {value}`) and `order_items`
(entity `:Product {id}`) into the same graph intentionally produces the
`mixed_key_shapes` review finding on `:Product` — that collision is the
fixture for graph-insights' collision detector.

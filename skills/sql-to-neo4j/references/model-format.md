# model.json format

`pg_inspect.py` writes this file; `load_graph.py` reads it. It is plain JSON and
meant to be edited by hand between the two steps. Everything the loader does is
driven by these fields, so tuning the graph is just editing this file.

## Top-level shape

```json
{
  "source":        { "table": "public.orders", "row_count": 10 },
  "mode":          "node_table",
  "primary_node":  { ... },
  "relationships": [ ... ],
  "columns":       [ { "name": "id", "type": "integer", "nullable": false }, ... ],
  "notes":         "Auto-inferred. Edit and re-run load_graph.py."
}
```

`columns` is the full column list (the loader uses it to know what to SELECT and
in what order). You normally don't touch it.

## primary_node

```json
"primary_node": {
  "label": "Order",
  "key_props": ["id"],
  "synthetic_key_from": null,
  "property_columns": ["id", "product", "amount", "created_at"]
}
```

- `label` — the node label for each row. Rename freely (e.g. `Order` → `PurchaseOrder`).
- `key_props` — the property/properties that uniquely identify a row node. Comes
  from the table's primary key. The loader puts a uniqueness constraint on each.
- `synthetic_key_from` — `null` when the table has a real PK. If the table had no
  PK, this is the list of columns hashed into a `_row_key` (and `key_props` is
  `["_row_key"]`). This keeps re-runs idempotent.
- `property_columns` — columns stored as properties on the row node. Columns that
  became dimension relationships are removed from here, and (since v0.3) so are
  FK columns — once a FK becomes an arrow, keeping the raw id too is double
  bookkeeping. PK columns always stay. Delete any others you don't want persisted.

## relationships

Each entry becomes `(:from_label)-[:rel_type]->(:to_label)` where the target node
is keyed on `to_key_prop` and its value comes from `via_column` of each row.

```json
{
  "kind": "foreign_key",         // or "dimension"
  "from_label": "Order",
  "rel_type": "HAS_CUSTOMER",
  "to_label": "Customer",
  "to_key_prop": "id",
  "via_column": "customer_id"
}
```

- `foreign_key` relationships come from real FK constraints; `to_key_prop` is the
  referenced column.
- `dimension` relationships come from low-cardinality text columns; `to_key_prop`
  is always `"value"` (the node looks like `(:Status {value:"shipped"})`).

Common edits: rename `rel_type` to something more natural (`HAS_STATUS` →
`IN_STATUS`), change `to_label`, delete a relationship you don't want, or add one
by hand following the same shape. Rows with a `NULL` in `via_column` simply don't
get that relationship.

## Tuning what becomes a dimension

If too many or too few columns became dimension nodes, re-run `pg_inspect.py` with:

- `--max-distinct-abs N` (default 50) — a column with more than N distinct values
  is never treated as categorical.
- `--max-distinct-ratio R` (default 0.1) — on large tables, distinct/rows must be
  below R. There's also a floor of 20 distinct values so small tables still catch
  obvious dimensions.

Or just edit `relationships` directly — the loader does exactly what the file says.

## Join-table mode (v0.3+)

When the table's primary key is exactly two FK columns, the whole model
switches shape: `"mode": "join_table"` and a `join_relationship` object
replaces `primary_node`:

```json
"join_relationship": {
  "rel_type": "ORDER_ITEM",
  "from_label": "Order",  "from_key_prop": "id", "from_via": "order_id",
  "to_label": "Product",  "to_key_prop": "id",  "to_via": "product_id",
  "property_columns": ["qty", "unit_price"]
}
```

Each row becomes `(:Order)-[:ORDER_ITEM {qty, unit_price}]->(:Product)`.
Rows with a NULL endpoint are skipped (a relationship needs both ends).
Rename `rel_type` or the endpoint labels freely. Self-referencing FKs in
node-table mode carry `"self_reference": true` on the relationship entry.

`pg_inspect.py` also writes `<out>.arrows.json` next to the model — import
it at https://arrows.app to edit the model as a diagram (it's a derived
visualization; the loader only ever reads `model.json`).

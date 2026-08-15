-- Live-verification fixture for pg-graph-toolkit.
-- Load into a scratch database:  psql "$DSN" -f tests/fixture.sql
-- Exercises: PK/FK inference, dimension promotion, join-table detection,
-- self-FK hierarchies, orphan detection, and the composite-FK regression
-- (a detail table that must NOT be classified as a join table).

DROP TABLE IF EXISTS shipment_details, shipments, order_items, employees, orders, products, customers CASCADE;

CREATE TABLE customers (id serial PRIMARY KEY, name text);
CREATE TABLE products  (id serial PRIMARY KEY, name text);

CREATE TABLE orders (
  id serial PRIMARY KEY,
  customer_id int REFERENCES customers(id),
  status varchar(20),          -- low cardinality -> dimension hub
  product text,                -- low cardinality -> dimension hub
  amount numeric(10,2),
  created_at timestamptz DEFAULT now()
);

-- join table: 2-column PK, two DISTINCT FK constraints -> mode join_table
CREATE TABLE order_items (
  order_id int REFERENCES orders(id),
  product_id int REFERENCES products(id),
  qty int, unit_price numeric(10,2),
  PRIMARY KEY (order_id, product_id)
);

-- self-FK -> hierarchy; 'Loner' has no manager and no reports -> orphan node
CREATE TABLE employees (id serial PRIMARY KEY, name text, manager_id int REFERENCES employees(id));

-- composite-FK regression: shipment_details' 2-column PK is ONE composite FK
-- to shipments -> must stay node_table, never join_table
CREATE TABLE shipments (region text, seq int, carrier text, PRIMARY KEY (region, seq));
CREATE TABLE shipment_details (
  region text, seq int, note text,
  PRIMARY KEY (region, seq),
  FOREIGN KEY (region, seq) REFERENCES shipments(region, seq)
);

INSERT INTO customers (name) SELECT 'Customer ' || g FROM generate_series(1,8) g;
INSERT INTO customers (name) VALUES  -- planted duplicates for graph-dedupe
 ('Globex LLC'), ('Globex L.L.C.'), ('Jonathan Smith'), ('Jonathon Smith');
INSERT INTO products  (name) SELECT 'Product '  || g FROM generate_series(1,5) g;
INSERT INTO orders (customer_id, status, product, amount)
SELECT (g % 8) + 1,
       (ARRAY['shipped','pending','cancelled'])[(g % 3) + 1],
       'Product ' || ((g % 5) + 1),
       (g * 7.5)::numeric(10,2)
FROM generate_series(1,60) g;
INSERT INTO order_items
SELECT o.id, p.id, (o.id + p.id) % 4 + 1, p.id * 3.25
FROM orders o JOIN products p ON (o.id + p.id) % 3 = 0;
INSERT INTO employees (name, manager_id) VALUES
 ('CEO', NULL), ('VP Eng', 1), ('VP Sales', 1), ('Eng Lead', 2), ('Dev A', 4),
 ('Dev B', 4), ('Sales Lead', 3), ('AE 1', 7), ('AE 2', 7), ('Loner', NULL);
INSERT INTO shipments VALUES ('EU', 1, 'DHL'), ('EU', 2, 'UPS'), ('US', 1, 'FedEx');
INSERT INTO shipment_details VALUES ('EU', 1, 'fragile'), ('US', 1, 'oversize');

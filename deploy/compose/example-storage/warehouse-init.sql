-- Sample governed data. Analysts get a read-only role; the admin registers it in Floren Data Platform.
CREATE TABLE sales (
    id         serial PRIMARY KEY,
    sold_at    date NOT NULL,
    region     text NOT NULL,
    product    text NOT NULL,
    amount_eur numeric(12, 2) NOT NULL
);

INSERT INTO sales (sold_at, region, product, amount_eur)
SELECT date '2026-01-01' + (g % 270),
       (ARRAY['North', 'South', 'East', 'West'])[1 + g % 4],
       (ARRAY['Basic', 'Plus', 'Pro'])[1 + g % 3],
       round((50 + random() * 950)::numeric, 2)
FROM generate_series(1, 5000) AS g;

CREATE ROLE analyst_ro LOGIN PASSWORD 'analyst_ro';
GRANT CONNECT ON DATABASE finance TO analyst_ro;
GRANT USAGE ON SCHEMA public TO analyst_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro;

-- dbt builds here (Floren Data Platform data source finance-dw-transform). Analysts can read what it builds.
CREATE ROLE transformer LOGIN PASSWORD 'transformer';
GRANT CONNECT, CREATE ON DATABASE finance TO transformer;
GRANT USAGE ON SCHEMA public TO transformer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO transformer;
ALTER DEFAULT PRIVILEGES FOR ROLE transformer GRANT USAGE ON SCHEMAS TO analyst_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE transformer GRANT SELECT ON TABLES TO analyst_ro;

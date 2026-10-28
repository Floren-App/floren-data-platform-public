"""Load the warehouse's sales into an Iceberg table on RustFS, and drop a JSON summary + a note as
plain files next to it. Run it on a warehouse; dbt (lake-models) builds on the Iceberg table."""

import datetime as dt

import pandas as pd

import fdp

args = fdp.args()
namespace = args.get("namespace", "finance")

sales = pd.read_sql("select id, sold_at, region, product, amount_eur from sales", fdp.connect("finance-dw"))
print(f"read {len(sales):,} sales rows from finance-dw")

# Trino performs the cross-catalog transfer; no lake keys or local Iceberg engine.
quoted_namespace = '"' + namespace.replace('"', '""') + '"'
engine = fdp.connect("lake", schema=namespace)
with engine.connect() as con:
    con.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS lake.{quoted_namespace}")
    con.exec_driver_sql(f"DROP TABLE IF EXISTS lake.{quoted_namespace}.sales")
    con.exec_driver_sql(f"CREATE TABLE lake.{quoted_namespace}.sales AS SELECT * FROM finance.public.sales")
    rows = con.exec_driver_sql(f"SELECT count(*) FROM lake.{quoted_namespace}.sales").scalar_one()
print(f"wrote Iceberg table lake.{namespace}.sales ({rows:,} rows)")

# Plain files: JSON and text, for whoever consumes them (R, dashboards, other teams).
files = fdp.files("lake-files")
summary = (sales.groupby("region").amount_eur.agg(["count", "sum"]).round(2)
           .rename(columns={"count": "sales", "sum": "revenue_eur"}).reset_index().to_dict("records"))
loaded_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
print("wrote", files.write_json("summaries/sales_by_region.json", {"loaded_at": loaded_at, "regions": summary}))
print("wrote", files.write_text("notes/last_load.txt", f"Loaded {rows} rows into lake.{namespace}.sales at {loaded_at}\n"))
print("files now:", [f["path"] for f in files.list()])

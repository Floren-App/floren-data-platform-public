"""Sales per region for a period. Arguments come from fdp.json (also accepted as CLI flags)."""

import os

import pandas as pd
from sqlalchemy import text
from tabulate import tabulate

import fdp

args = fdp.args()
out = fdp.output_dir()

query = """
    select sold_at, region, product, amount_eur
    from sales
    where sold_at between cast(:start as date) and cast(:end as date)
"""
params = {"start": args["start_date"], "end": args["end_date"]}
print(f"Querying finance-dw for {args['start_date']} .. {args['end_date']}")
df = pd.read_sql(text(query), fdp.connect("finance-dw"), params=params)
if args["region"] != "All":
    df = df[df.region == args["region"]]
print(f"{len(df):,} sales rows")

by_region = df.groupby("region", as_index=False).amount_eur.sum().sort_values("amount_eur", ascending=False)
top = df.groupby("product", as_index=False).amount_eur.sum().nlargest(args["top"], "amount_eur")

print(tabulate(by_region, headers="keys", tablefmt="github", showindex=False, floatfmt=",.0f"))
by_region.to_csv(os.path.join(out, "sales_by_region.csv"), index=False)
top.to_csv(os.path.join(out, "top_products.csv"), index=False)

if args["chart"]:
    import matplotlib

    matplotlib.use("Agg")
    ax = by_region.plot.bar(x="region", y="amount_eur", legend=False, title="Sales per region (EUR)")
    ax.figure.tight_layout()
    ax.figure.savefig(os.path.join(out, "sales_by_region.png"), dpi=120)
    print("chart written")

print("done")

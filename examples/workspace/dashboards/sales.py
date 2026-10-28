import pandas as pd
import streamlit as st

import fdp

st.set_page_config(page_title="Sales", layout="wide")
st.title("Sales dashboard")


def load() -> pd.DataFrame:
    viewer = st.context.headers.get("X-FDP-Viewer")
    engine = fdp.connect("finance-dw", viewer_token=viewer)
    try:
        return pd.read_sql("select sold_at, region, product, amount_eur from sales", engine)
    finally:
        engine.dispose()


df = load()
region = st.selectbox("Region", ["All", *sorted(df.region.unique())])
if region != "All":
    df = df[df.region == region]
c1, c2 = st.columns(2)
c1.metric("Sales (EUR)", f"{df.amount_eur.sum():,.0f}")
c2.metric("Orders", f"{len(df):,}")
st.bar_chart(df.groupby("product").amount_eur.sum())

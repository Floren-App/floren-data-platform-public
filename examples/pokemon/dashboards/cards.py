"""Caller-bound data product: every query carries the current viewer token."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import streamlit as st
from pokemon_pipeline.product_queries import query_product

st.set_page_config(page_title='Pokémon Card Explorer', page_icon='⚡', layout='wide')
st.title('Pokémon Card Explorer')
st.caption(f"{os.environ['POKEMON_ENV'].upper()} · English cards · Governed data product")

def query(table):
    result = query_product(os.environ['POKEMON_SOURCE'], table,
                           st.context.headers.get('X-FDP-Viewer'))
    return pd.DataFrame(result['rows'], columns=result['columns'])

st.button('Refresh data')  # Streamlit reruns; no shared cross-viewer data cache.
try:
    cards, summary = query('cards_current'), query('set_summary')
except Exception:
    st.info('The data product is not ready. An engineer must run the pipeline successfully first.')
    st.stop()
if cards.empty:
    st.info('No published cards yet.'); st.stop()
a,b,c = st.columns(3)
a.metric('Cards',len(cards)); b.metric('Sets',len(summary)); c.metric('Rarities',cards.rarity.nunique())
st.caption('Last observed: ' + str(cards.last_seen_at.max()))
sets = st.multiselect('Sets', sorted(cards.set_name.dropna().unique()), default=sorted(cards.set_name.dropna().unique()))
search = st.text_input('Find a card', placeholder='Pikachu, Charizard…')
filtered = cards[cards.set_name.isin(sets)]
if search:
    filtered = filtered[filtered.card_name.str.contains(search,case=False,regex=False,na=False)]
st.dataframe(summary,hide_index=True,use_container_width=True)
if len(filtered):
    st.bar_chart(filtered.groupby('primary_type',dropna=False).size().rename('Cards'))
st.dataframe(filtered[['record_id','card_name','set_name','card_number','rarity','primary_type','hit_points','price_trend_eur']],
             hide_index=True,use_container_width=True)
st.download_button('Download filtered product',filtered.to_csv(index=False),'pokemon-cards.csv','text/csv')
st.caption('Source: TCGdex. Prices are optional upstream metadata. Pokémon belongs to its respective rights holders.')

st.subheader('Streaming synthetic quotes')
st.caption('Synthetic EUR demo values; not live market prices. The table refreshes on the two-minute price DAG.')
try:
    st.dataframe(query('card_prices_current'), hide_index=True, use_container_width=True)
except Exception:
    st.info('The optional price product is not available yet, or you do not have access.')

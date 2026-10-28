"""Analyst data product. This identity can read only the reporting schema."""
import os
import pandas as pd
import streamlit as st
import fdp

st.set_page_config(page_title='Pokémon Card Explorer', page_icon='⚡', layout='wide')
st.title('Pokémon Card Explorer')
st.caption(f"{os.environ['POKEMON_ENV'].upper()} · English cards · Governed data product")

@st.cache_data(ttl=60)
def query(table):
    # Fixed allowlist: no arbitrary SQL or schema names from UI input.
    if table not in ('cards_current', 'set_summary'):
        raise ValueError('unknown product')
    result = fdp.query(os.environ['POKEMON_SOURCE'], f'SELECT * FROM reporting.{table}', max_rows=10000)
    return pd.DataFrame(result['rows'], columns=result['columns'])

if st.button('Refresh data'):
    st.cache_data.clear()
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

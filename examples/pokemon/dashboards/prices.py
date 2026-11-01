"""Stream-operator dashboard: fixed Reader of synthetic price events only."""
import os
import pandas as pd
import streamlit as st
import fdp

st.set_page_config(page_title='Pokemon Price Operations', page_icon='📈', layout='wide')
st.title('Pokemon Price Operations')
st.caption(f"{os.environ['POKEMON_ENV'].upper()} · Synthetic EUR quotes · No personal card records")
st.button('Refresh')
try:
    result = fdp.query(os.environ['POKEMON_SOURCE'],
        'SELECT event_id, card_id, card_name, observed_at, price_minor, currency, price_source, '
        'topic, partition_id, offset_id, ingested_at FROM pokemon_prices.price_events '
        'ORDER BY ingested_at DESC LIMIT 2000', max_rows=2000)
except Exception:
    st.info('No price table available. Run price-bootstrap and start the managed sink first.')
    st.stop()
events = pd.DataFrame(result['rows'], columns=result['columns'])
if events.empty:
    st.info('Waiting for the first synthetic quote publication.')
    st.stop()
canonical = events.drop_duplicates(subset=['event_id'])
a, b, c = st.columns(3)
a.metric('Distinct events in sample', len(canonical))
b.metric('Cards in sample', canonical.card_id.nunique())
c.metric('Replay rows in sample', len(events) - len(canonical))
st.caption('Bounded sample of the latest 2,000 rows. Offset progress is landed SQL evidence, not broker consumer lag.')
chart = canonical.assign(price_eur=canonical.price_minor / 100.0).pivot_table(
    index='observed_at', columns='card_name', values='price_eur', aggfunc='last')
st.line_chart(chart)
st.subheader('Last landed offset per partition')
st.dataframe(events.groupby(['topic', 'partition_id'], as_index=False).offset_id.max(), hide_index=True)
st.subheader('Event provenance')
st.dataframe(events, hide_index=True, use_container_width=True)

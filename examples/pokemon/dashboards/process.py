"""Engineer-only operational flow, SCD2, process history and file traceability."""
import os
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import fdp

st.set_page_config(page_title='Pokémon Pipeline Monitor',page_icon='🔎',layout='wide')
st.title('Pokémon Pipeline Monitor')
st.caption(f"{os.environ['POKEMON_ENV'].upper()} · Engineer process flow · raw data stays append-only")
source = os.environ['POKEMON_SOURCE']

@st.cache_data(ttl=20)
def query(sql, params=None):
    result = fdp.query(source,sql,params,max_rows=2000)
    return pd.DataFrame(result['rows'],columns=result['columns'])

if st.button('Refresh monitoring'):
    st.cache_data.clear()
figure = go.Figure()
names = ['Managed API','Immutable S3','Append-only raw','dbt + SCD2','Data product']
for i in range(4):
    figure.add_annotation(x=i+0.9,y=0,ax=i+0.15,ay=0,xref='x',yref='y',axref='x',ayref='y',showarrow=True,arrowhead=3)
figure.add_trace(go.Scatter(x=list(range(5)),y=[0]*5,mode='markers+text',text=names,textposition='top center',
                           marker=dict(size=28,color='#2563eb'),hoverinfo='text'))
figure.update_layout(height=180,showlegend=False,margin=dict(l=50,r=50,t=30,b=20),
                     xaxis=dict(visible=False,range=[-.5,4.5]),yaxis=dict(visible=False,range=[-.5,.5]))
st.plotly_chart(figure,use_container_width=True)
st.caption('Catalog lineage adds the actual API calls, SDK file accesses, SQL, dbt model graph and Airflow orchestration overlay.')
try:
    commits = query('SELECT * FROM pokemon_raw.load_commits ORDER BY committed_at DESC LIMIT 100')
    events = query('SELECT * FROM pokemon_raw.process_events ORDER BY event_at DESC LIMIT 500')
except Exception:
    st.info('Run the bootstrap job once, then trigger the Airflow DAG.'); st.stop()
if commits.empty:
    st.warning('No complete raw load yet. Partial attempts and failures remain visible below.')
else:
    latest = commits.iloc[0]
    a,b,c = st.columns(3)
    a.metric('Latest records',int(latest.record_count)); b.metric('Latest files',int(latest.file_count))
    c.metric('Latest committed batch',latest.batch_id[:12])
flow, history, trace = st.tabs(['Process history','Full SCD2 history','File and record trace'])
with flow:
    st.dataframe(events,hide_index=True,use_container_width=True)
    st.dataframe(commits,hide_index=True,use_container_width=True)
    st.caption('Started without a terminal event means incomplete/unknown, not successful. Airflow keeps task logs and retry history.')
with history:
    rid = st.text_input('Stable record ID (copy from the product or raw table)',key='history_record')
    if rid:
        st.dataframe(query('SELECT * FROM pokemon_history.record_history WHERE record_id=? ORDER BY version_number',[rid]),
                     hide_index=True,use_container_width=True)
with trace:
    batch = st.selectbox('Batch',list(commits.batch_id.unique())) if len(commits) else None
    if batch:
        st.dataframe(query('SELECT * FROM pokemon_raw.files WHERE batch_id=? ORDER BY source_path',[batch]),
                     hide_index=True,use_container_width=True)
        evidence = query('SELECT record_id,observation_id,entity_type,json_pointer,file_id,file_sha256,source_file_uri,load_id '
                         'FROM pokemon_raw.records WHERE batch_id=? ORDER BY entity_type,record_id LIMIT 500',[batch])
        st.dataframe(evidence,hide_index=True,use_container_width=True)
    rid = st.text_input('Trace all source files for record ID',key='file_record')
    if rid:
        st.dataframe(query('SELECT * FROM pokemon_raw.records WHERE record_id=? ORDER BY observed_at,loaded_at',[rid]),
                     hide_index=True,use_container_width=True)
st.caption('Displays at most 2,000 rows per query. This app never mutates data or exposes storage credentials.')

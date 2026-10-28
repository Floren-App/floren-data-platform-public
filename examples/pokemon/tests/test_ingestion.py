import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('ingest',Path(__file__).parents[1]/'ingestion/ingest.py')
ingest=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


def test_volatile_metadata_does_not_create_false_scd2_changes():
    a={'id':'base1-1','name':'Alakazam','updated':'old','pricing':{'updated':'old','trend':4}}
    b={'name':'Alakazam','id':'base1-1','updated':'new','pricing':{'updated':'new','trend':4}}
    assert ingest.observation(a)[1]==ingest.observation(b)[1]
    b['pricing']['trend']=5
    assert ingest.observation(a)[1]!=ingest.observation(b)[1]


def test_nested_semantic_changes_are_preserved_and_original_payload_retained():
    original={'id':'x','attacks':[{'name':'Thunder','damage':40}],'updated':'yesterday'}
    text,digest=ingest.observation(original)
    assert 'yesterday' in text
    changed={**original,'attacks':[{'name':'Thunder','damage':50}]}
    assert digest!=ingest.observation(changed)[1]
    assert original['attacks'][0]['damage']==40

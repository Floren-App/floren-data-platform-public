"""The portable example preserves IDs and batch traceability across replay."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[3]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'examples/kafka'/f'{name}.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_landing_batches_records_and_keeps_replay_identity_and_file_trace():
    landing=load('land_cards')
    writes=[]
    files=SimpleNamespace(write_json=lambda path,rows:writes.append((path,rows)))
    records=[SimpleNamespace(topic='pokemon.cards',partition=0,offset=i,timestamp=1,
        value={'record_id':f'card-{i}','event_id':f'event-{i}','payload':{'name':f'Card {i}'}}) for i in range(200)]
    path=landing.land(files,records)
    assert len(writes)==1 and len(writes[0][1])==200
    assert all(row['file_id'] in path for row in writes[0][1])
    assert writes[0][1][123]['record_id']=='card-123'
    assert writes[0][1][123]['kafka']=={'topic':'pokemon.cards','partition':0,'offset':123,'timestamp':1}
    assert landing.land(files,list(reversed(records)))==path
    assert writes[0]==writes[1]
    records[0].value={'payload':{}}
    with pytest.raises(ValueError,match='Invalid card event'):
        landing.land(files,records)
    assert len(writes)==2, 'A poison record must not silently disappear into a partial committed batch'


def test_publisher_keeps_card_and_content_ids_stable_across_observations():
    publisher=load('publish_cards')
    api=SimpleNamespace(get=lambda path:SimpleNamespace(raise_for_status=lambda:None,
        json=lambda:{'id':'base1','cards':[{'id':'base1-1','name':'Alakazam'}]}))
    events=[]
    producer=SimpleNamespace(send=lambda value,**kwargs:events.append((value,kwargs)) or SimpleNamespace(get=lambda **kwargs:None))
    assert publisher.publish(api,producer,['base1'])==1
    assert publisher.publish(api,producer,['base1'])==1
    assert events[0][0]['event_id']==events[1][0]['event_id']
    assert events[0][0]['record_id']=='base1-1' and events[0][1]['key']==b'base1-1'
    assert events[0][0]['source_path']=='/sets/base1'

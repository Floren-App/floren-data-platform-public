#!/usr/bin/env python3
"""Test-only integration probe: A→B→B→A, failed batches and deterministic rebuilds.

Uses a reserved synthetic card in Test only and removes it in a finally block, then rebuilds.
Production is never modified by this probe.
"""
import json
import uuid
import verify as v


def main():
    source='pokemon-test'
    card='fdp-scd2-probe-'+uuid.uuid4().hex[:8]
    batch_prefix='scd2-'+uuid.uuid4().hex
    try:
        for i,name in enumerate(['Probe A','Probe B','Probe B','Probe A','UNCOMMITTED']):
            payload=json.dumps({'id':card,'localId':'999','name':name,'set':{'id':'base1'},'category':'Pokemon'})
            batch=batch_prefix+'-'+str(i)
            stamp=f'2000-01-0{i+1} 00:00:00.000000'
            v.sql(source,'INSERT INTO raw.card_observations VALUES (?,?,?,?,?,CAST(? AS timestamp(6)))',parameters=[batch,card,'base1',payload,name,stamp])
            if i<4:
                v.sql(source,'INSERT INTO raw.ingestion_batches VALUES (?,CAST(? AS timestamp(6)),?,?)',parameters=[batch,stamp,167,2])
        for attempt in range(2):
            v.execute('test','models',{'command':'run'})
            result=v.sql(source,'SELECT card_name,is_current FROM analytics.dim_cards_history WHERE card_id=? ORDER BY valid_from',parameters=[card])
            assert result['rows']==[['Probe A',False],['Probe B',False],['Probe A',True]],result
            print('SCD2 A→B→B→A and uncommitted exclusion passed, rebuild',attempt+1,flush=True)
    finally:
        v.sql(source,'DELETE FROM raw.card_observations WHERE entity_id=?',parameters=[card])
        v.sql(source,'DELETE FROM raw.ingestion_batches WHERE batch_id LIKE ?',parameters=[batch_prefix+'%'])
        v.execute('test','models',{'command':'build'})
        assert v.sql(source,'SELECT count(*) FROM analytics.cards_current')['rows']==[[166]]
        print('Test fixture removed; six models and fourteen tests restored',flush=True)

if __name__=='__main__':main()

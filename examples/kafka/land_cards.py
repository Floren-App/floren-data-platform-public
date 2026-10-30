"""At-least-once JSON batches. Topic/partition/offset is the ingestion deduplication key."""
import hashlib
import json
import os

import fdp


def land(files, records):
    if not records:
        return None
    offsets = sorted((r.topic,r.partition,r.offset) for r in records)
    batch_id = hashlib.sha256(json.dumps(offsets,separators=(',',':')).encode()).hexdigest()
    rows = []
    for record in sorted(records,key=lambda r:(r.topic,r.partition,r.offset)):
        value = record.value
        if not isinstance(value,dict) or not all(value.get(key) for key in ('record_id','event_id','payload')):
            # Fail the stream without committing; do not silently discard a poison record.
            raise ValueError('Invalid card event; quarantine/replay requires an explicit operator decision')
        rows.append({'file_id':batch_id,'record_id':value['record_id'],'event_id':value['event_id'],
            'kafka':{'topic':record.topic,'partition':record.partition,'offset':record.offset,'timestamp':record.timestamp},
            'event':value})
    path='pokemon/kafka/batches/'+batch_id+'.json'
    files.write_json(path,rows)
    return path


if __name__ == '__main__':
    files=fdp.files(os.environ['POKEMON_LANDING'])
    with fdp.kafka.consumer(os.environ['POKEMON_TOPIC'],group='pokemon-landing',
                            auto_offset_reset='earliest',max_poll_records=250) as consumer:
        while True:
            batches=consumer.poll(timeout_ms=1000)
            records=[record for batch in batches.values() for record in batch]
            if records:
                path=land(files,records)
                consumer.commit()  # only after every record from this poll is durably written
                print(f'Landed {len(records)} events into {path}',flush=True)

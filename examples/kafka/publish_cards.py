"""Small card-inventory observations, not invented card prices. Run on a schedule."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re

import fdp


def publish(api, producer, set_ids):
    if not 1 <= len(set_ids) <= 3 or len(set(set_ids)) != len(set_ids):
        raise ValueError('Choose between one and three distinct English sets')
    if not all(re.fullmatch(r'[a-z0-9.-]+', value) for value in set_ids):
        raise ValueError('Invalid set allowlist')
    observed = datetime.now(timezone.utc).isoformat()
    count = 0
    for set_id in set_ids:
        response = api.get('/sets/'+set_id)
        response.raise_for_status()
        inventory = response.json()
        if not isinstance(inventory, dict) or inventory.get('id') != set_id or not isinstance(inventory.get('cards'),list):
            raise ValueError('Unexpected set response')
        for card in inventory['cards']:
            if not isinstance(card,dict) or not re.fullmatch(r'[A-Za-z0-9.-]+',str(card.get('id',''))):
                raise ValueError('Unexpected card identity')
            payload = {'set_id':set_id,'card':card}
            canonical = json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
            event = {'version':1,'record_id':card['id'],
                'event_id':hashlib.sha256(b'tcgdex:en:'+canonical).hexdigest(),
                'observed_at':observed,'source_api':'tcgdex','source_path':'/sets/'+set_id,
                'run_id':os.environ.get('FDP_RUN_ID',''),'payload':payload}
            producer.send(event,key=card['id'].encode()).get(timeout=30)
            count += 1
    return count


if __name__ == '__main__':
    with fdp.kafka.producer(os.environ['POKEMON_TOPIC']) as producer:
        count = publish(fdp.api(os.environ['POKEMON_API']),producer,
                        os.environ.get('POKEMON_SETS','base1,base2').split(','))
    print(f'Acknowledged {count} card inventory observations')

"""Airflow's terminal pipeline result, appended even when upstream tasks failed."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdp
from pokemon_pipeline.contracts import batch_id, identifier
from ingestion.load_raw import event

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--status', choices=['succeeded','failed'], required=True)
    parser.add_argument('--task-history', default='[]')
    args = parser.parse_args()
    query = lambda sql, parameters=None: fdp.query(os.environ['POKEMON_SOURCE'], sql, parameters)
    history = json.loads(args.task_history)
    if not isinstance(history,list) or len(history)>10:
        raise ValueError('invalid task history')
    for task in history:
        if task['task_id'] not in ('extract_api_to_s3','load_s3_to_raw','build_history_and_quality','build_products'):
            raise ValueError('unknown pipeline stage')
        event(query, identifier(os.getenv('POKEMON_RAW_SCHEMA','pokemon_raw')),
              batch_id(os.environ['POKEMON_ENV'],args.run_id),args.run_id,task['task_id'],task['state'],
              detail=json.dumps(task), execution_id=task.get('execution_id',''))
    event(query, identifier(os.getenv('POKEMON_RAW_SCHEMA','pokemon_raw')),
          batch_id(os.environ['POKEMON_ENV'], args.run_id), args.run_id, 'pipeline', args.status)

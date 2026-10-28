"""Managed API → immutable S3 files → raw records → tested history → data products."""

import os

import pendulum
from airflow.decorators import dag
from airflow.utils.trigger_rule import TriggerRule
from fdp.airflow import FDPRunOperator

from pokemon_tasks import ProcessResultOperator

ENVIRONMENT = os.environ['POKEMON_ENV']


@dag(
    dag_id=f'pokemon_{ENVIRONMENT}_daily',
    description='Load Pokémon cards, preserve their history, and publish tested data products.',
    schedule='0 5 * * *',
    start_date=pendulum.datetime(2026, 1, 1, tz='UTC'),
    catchup=False,
    max_active_runs=1,
    default_args={'retries': 2, 'retry_delay': pendulum.duration(minutes=2)},
    tags=['Pokemon', ENVIRONMENT],
)
def pokemon_daily():
    """Run each stage on its FDP compute pool, using this environment's service identity.

    The raw loader splits each immutable JSON list into individual records. dbt then
    builds engineering history and tests it before publishing analyst-facing tables.
    Always record the stage outcomes, including failures, in the process history.
    """
    extract = FDPRunOperator(
        task_id='extract_api_to_s3',
        workload=f'pokemon-extract-{ENVIRONMENT}',
        args={'run_id': '{{ run_id }}'},
        doc_md='Fetch the managed TCGDex API and save immutable JSON batch files to S3.',
    )
    load = FDPRunOperator(
        task_id='load_s3_to_raw',
        workload=f'pokemon-load-{ENVIRONMENT}',
        args={'run_id': '{{ run_id }}'},
        doc_md='Append raw records with stable record IDs, file references and load metadata.',
    )
    history = FDPRunOperator(
        task_id='build_history_and_quality',
        workload=f'pokemon-models-{ENVIRONMENT}',
        args={'command': 'build', 'select': 'tag:engineering'},
        doc_md='Build SCD2 history and run engineering data-quality tests.',
    )
    products = FDPRunOperator(
        task_id='build_products',
        workload=f'pokemon-models-{ENVIRONMENT}',
        args={'command': 'build', 'select': 'tag:product'},
        doc_md='Build and test the reporting tables used by the analyst dashboard.',
    )
    record_result = ProcessResultOperator(
        task_id='record_pipeline_result',
        workload=f'pokemon-process-event-{ENVIRONMENT}',
        stage_ids=[extract.task_id, load.task_id, history.task_id, products.task_id],
        trigger_rule=TriggerRule.ALL_DONE,
        doc_md='Append the pipeline outcome and each stage’s execution ID to process history.',
    )

    extract >> load >> history >> products
    [extract, load, history, products] >> record_result


pokemon_daily()

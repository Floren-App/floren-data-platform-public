"""Bounded quote publication and periodic marts; the sink is a separate stream workload."""
import os
import pendulum
from airflow.decorators import dag
from fdp.airflow import FDPRunOperator

ENVIRONMENT = os.environ['POKEMON_ENV']

if os.getenv('POKEMON_STREAMING_ENABLED') == 'true':
    @dag(dag_id=f'pokemon_{ENVIRONMENT}_prices', schedule='*/2 * * * *',
         start_date=pendulum.datetime(2026, 1, 1, tz='UTC'), catchup=False,
         max_active_runs=1, default_args={'retries': 2}, tags=['Pokemon', 'Synthetic prices', ENVIRONMENT])
    def prices():
        publish = FDPRunOperator(task_id='publish_quotes', workload=f'pokemon-price-publish-{ENVIRONMENT}',
                                 args={'run_id': '{{ run_id }}', 'observed_at': '{{ ts }}'})
        landed = FDPRunOperator(task_id='wait_for_durable_quotes', workload=f'pokemon-price-await-{ENVIRONMENT}',
                                 args={'run_id': '{{ run_id }}', 'observed_at': '{{ ts }}'})
        build = FDPRunOperator(task_id='publish_price_table', workload=f'pokemon-price-models-{ENVIRONMENT}',
                               args={'command': 'build', 'select': 'tag:prices'})
        publish >> landed >> build
    prices()

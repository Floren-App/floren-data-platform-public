"""Daily lake pipeline: load the Iceberg table, then build dbt models and the R report in parallel."""

import pendulum
from airflow import DAG

from fdp.airflow import FDPRunOperator

with DAG(
    dag_id="lake_daily",
    schedule="@daily",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    tags=["lake"],
) as dag:
    load = FDPRunOperator(task_id="load_lake", program="load-lake")
    models = FDPRunOperator(task_id="lake_models", program="lake-models",
                                  args={"command": "build"})
    report = FDPRunOperator(task_id="lake_report", program="lake-report")
    load >> [models, report]   # these two run at the same time on different workers

"""Daily finance pipeline: build the dbt models, then the sales report.

Airflow only orchestrates. Each task runs on a Floren Data Platform warehouse, and its console appears in
the task log. Programs are referenced by their names in fdp.json (same repository).
"""

import pendulum
from airflow import DAG

from fdp.airflow import FDPRunOperator

with DAG(
    dag_id="finance_daily",
    schedule="@daily",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    tags=["finance"],
) as dag:
    models = FDPRunOperator(
        task_id="build_models",
        program="finance-models",
        args={"command": "build", "select": "+tag:daily"},
    )
    report = FDPRunOperator(
        task_id="sales_report",
        program="sales-report",
        args={"start_date": "2026-01-01", "end_date": "{{ ds }}", "region": "All", "chart": True},
        warehouse="small",
    )
    models >> report

"""Process-history reporting for the Pokémon DAG; no work runs during DAG parsing."""

import json

from fdp.airflow import FDPRunOperator


class ProcessResultOperator(FDPRunOperator):
    """Record every stage outcome, even when an upstream task failed or was skipped.

    Keep FDP's native operator lifecycle: managed identity, remote logs, cancellation,
    workload lineage and the execution ID returned through XCom.
    """

    def __init__(self, *, stage_ids, **kwargs):
        super().__init__(**kwargs)
        self.stage_ids = stage_ids

    def execute(self, context):
        instances = {
            task.task_id: task
            for task in context['dag_run'].get_task_instances()
        }
        history = []
        for task_id in self.stage_ids:
            task = instances[task_id]
            history.append({
                'task_id': task_id,
                'state': task.state or 'unknown',
                'try_number': task.try_number,
                'start_date': str(task.start_date or ''),
                'end_date': str(task.end_date or ''),
                'execution_id': context['ti'].xcom_pull(task_ids=task_id) or '',
            })

        self.args = {
            'run_id': context['run_id'],
            'status': 'succeeded' if all(task['state'] == 'success' for task in history) else 'failed',
            'task_history': json.dumps(history),
        }
        return super().execute(context)

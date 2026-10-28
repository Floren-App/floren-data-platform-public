"""Airflow 2.x native lineage backend. Uses rendered inlets/outlets and the native DAG graph."""
import logging
import hashlib
import json
import time
from airflow.lineage.backend import LineageBackend

from .lineage import airflow_dag, airflow_payload, send


class FDPLineageBackend(LineageBackend):
    def send_lineage(self, operator, inlets=None, outlets=None, context=None):
        try:
            airflow_dag(operator.dag, operator, inlets, outlets)
        except Exception:
            # Metadata outages cannot turn a successful data task into a failed task.
            logging.getLogger(__name__).warning("Floren Data Platform catalog lineage upload failed", exc_info=False)


def sync_serialized_dags(seen):
    """Read Airflow's own parsed metadata, without executing DAG files or running tasks."""
    from airflow.models.serialized_dag import SerializedDagModel
    from airflow.utils.session import create_session
    with create_session() as session:
        for record in session.query(SerializedDagModel).all():
            payload = airflow_payload(record.dag)
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            if seen.get(record.dag_id) != fingerprint:
                send("airflow", payload)
                seen[record.dag_id] = fingerprint


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    seen = {}
    while True:
        try:
            sync_serialized_dags(seen)
        except Exception:
            logging.getLogger(__name__).warning("Floren Data Platform serialized DAG metadata sync failed", exc_info=False)
        if not args.watch:
            break
        time.sleep(30)

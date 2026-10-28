"""Measurements use the caller's managed identity and layer through Floren Data Platform."""
from urllib.parse import quote
from . import _client


def measure_metric(asset_id, metric_id, token=None, viewer_token=None):
    """Evaluate a saved SQL KPI through governed Trino; return its observation."""
    with _client(token) as client:
        response = client.post(f'/v1/catalog/assets/{quote(asset_id, safe="")}/metrics/{quote(metric_id, safe="")}/measure',
            headers={'X-FDP-Viewer':viewer_token} if viewer_token else {})
        response.raise_for_status()
        return response.json()


def report_metric(asset_id, metric_id, value, *, numerator=None, denominator=None, observed_at=None, idempotency_key=None, token=None):
    """Report a finite numeric result. Requires Contributor; thresholds run server-side."""
    body = {'value': value, 'numerator': numerator, 'denominator': denominator, 'idempotency_key': idempotency_key}
    if observed_at:
        body['observed_at'] = observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at
    with _client(token) as client:
        response = client.post(f'/v1/catalog/assets/{quote(asset_id, safe="")}/metrics/{quote(metric_id, safe="")}/observations', json=body)
        response.raise_for_status()
        return response.json()


def observe_dataset(asset_id, *, kind='checked', idempotency_key, observed_at=None, token=None):
    """Record a successful check, materialization, or source watermark separately."""
    body = {'kind': kind, 'idempotency_key': idempotency_key}
    if observed_at:
        body['observed_at'] = observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at
    with _client(token) as client:
        response = client.post(f'/v1/catalog/assets/{quote(asset_id, safe="")}/observations', json=body)
        response.raise_for_status()
        return response.json()

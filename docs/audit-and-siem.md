# Audit trail and SIEM

FDP records administrative and security events in its audit trail and every completed SQL query in its
query log. Both can be streamed to your SIEM or log platform, so security teams see who changed what
and who accessed which data, next to the rest of your monitoring.

## What is sent

| Source | Examples | Category |
|---|---|---|
| Audit trail | Role assignments and group bindings, secret and credential changes, service tokens, API configuration, API calls (including denied ones), runs and deployments, SQL submissions and failures, layer, storage and platform settings | The action prefix, for example `role`, `secret`, `api`, `sql` |
| Data access (`includeQueries`) | Every completed SQL query: the effective identity, initiator, run or workload, tables read and written, rows, duration and outcome | `data` |

SQL text is not sent unless the query log stores it (`fdp_query_log_settings`) **and**
`siem.includeSqlText` is on: query text can contain literal values. Secret values, tokens and request
bodies are never part of an event.

## Event format

Every destination receives the same JSON event (`schema: fdp.audit/1`):

```json
{
  "schema": "fdp.audit/1",
  "id": "audit:4821",
  "time": "2026-10-29T09:14:03.512Z",
  "installation": "fdp.example.com",
  "source": "audit",
  "category": "role",
  "action": "role.assign",
  "outcome": "success",
  "actor": "ana",
  "target": "schema-resource-id",
  "detail": {"role": "Reader", "group": "analysts"},
  "chain": {"seq": 4821, "prev": "9c1e…", "hash": "5b7a…"},
  "signature": "e3f0…"
}
```

`outcome` is `failure` for denied, refused, failed or unavailable operations; syslog and OpenTelemetry map that to
warning severity.

## Tamper evidence

Each destination receives a **hash chain**: `chain.hash` is the SHA-256 of the previous hash and the
event, and `chain.seq` counts upward without gaps. A removed, reordered or altered event breaks the chain.
With `FDP_SIEM_SIGNING_KEY` in the platform Secret, every chain hash is also signed with HMAC-SHA256, so
events cannot be forged without the key. Check an export with the standard-library verifier:

```sh
FDP_SIEM_SIGNING_KEY=... python3 scripts/verify-audit-chain.py export.ndjson
# 18342 events checked: chain intact
```

It accepts the generic and stdout format, Splunk HEC events and Elastic documents.

## Destinations

Configure one or more in the Helm values. Each keeps its own position and chain, so a failing
destination does not hold up the others.

| Destination | Settings | Use with |
|---|---|---|
| JSON lines on stdout | `siem.stdout: true` | Any log collector: Fluent Bit, Vector, the Splunk, Datadog or Elastic agents |
| Syslog | `siem.syslog.address`, `transport` (`tls`, `tcp`, `udp`), `facility`, `clientCertSecret` | QRadar, ArcSight, Microsoft Sentinel (through its syslog collector), Graylog, rsyslog |
| HTTPS collector | `siem.http.url`, `format` (`generic`, `splunk`, `elastic`), `index`; token `FDP_SIEM_HTTP_TOKEN` | Splunk HTTP Event Collector, Elasticsearch or OpenSearch bulk API, any JSON-lines webhook |
| OpenTelemetry logs | `siem.otlp.endpoint`; headers `FDP_SIEM_OTLP_HEADERS` | An OpenTelemetry Collector, and any backend it exports to |

```yaml
siem:
  syslog:
    address: siem.example.com:6514        # RFC 5424 over TLS, octet-counted (RFC 5425)
  http:
    url: https://splunk.example.com:8088/services/collector/event
    format: splunk                         # FDP_SIEM_HTTP_TOKEN: the HEC token
  otlp:
    endpoint: http://otel-collector.observability:4318
```

Syslog over TLS verifies the receiver against `trust.caBundleSecret` and the system roots; set
`clientCertSecret` (`tls.crt`, `tls.key`) for mutual TLS. Credentials live in the platform Secret
(`existingSecret`), never in values. The HTTPS collector authenticates with `Bearer`, `Splunk` or `ApiKey`
according to its format.

## Delivery

- **At least once.** A batch is marked delivered only after the destination accepted it. After a failure
  it is sent again; deduplicate on `id` if your platform needs exactly once.
- **Ordered.** Events are sent in commit order. If a database transaction commits an audit event late, the
  exporter waits for it (`siem_gap_wait_seconds`, default 60) rather than skipping it.
- **Leader only.** Exactly one control-plane replica exports, so events are not sent twice in normal
  operation.
- **Complete from the start.** A new destination receives the whole retained trail (`start: beginning`);
  use `start: now` to begin with new events only.

## Monitoring the export

`GET /api/v1/admin/siem` shows each destination, the events sent, how far it has exported and the last
error, without addresses or credentials. Prometheus metrics: `fdp_siem_events_sent_total`,
`fdp_siem_last_success_timestamp_seconds` and `fdp_siem_failing`, labelled by destination. Alert when a
destination fails or its last success is older than a few minutes:

```yaml
- alert: FDPSiemExportFailing
  expr: fdp_siem_failing == 1
  for: 10m
- alert: FDPSiemExportStale
  expr: time() - fdp_siem_last_success_timestamp_seconds > 900
```

The audit trail and query log remain in the platform metadata database as well. Retention of the query
log follows `fdp_query_log_settings`; keep long-term evidence in your SIEM.

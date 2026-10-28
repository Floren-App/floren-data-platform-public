# API management

Open **APIs** in Floren Data Platform. Internal and External are directions relative to the
platform, and both support REST and SOAP 1.1/1.2. Configuration, operations, recent
calls, permissions and an interactive test console live on the API's page.

## Internal: outside callers run deployed programs

1. Create an Internal API with a unique address, for example `finance-reports`.
2. Require JWT, mTLS, or both. When both are enabled, both must pass. For testing only, an API can
   instead allow anonymous callers (`inbound.anonymous = true`, "Allow anonymous callers"): anyone who
   reaches the API gateway can run its programs as the API's identity. The app shows a red warning on
   such APIs, enabling it is audited (`api.anonymous_enabled`), calls are recorded as `anonymous`, and it
   is refused in production layers.
3. Add operations targeting a deployed script, notebook or dbt program. An operation
   can belong to an endpoint group with inherited IAM.
4. Assign the API's managed identity (shown in Settings) Executor directly on the workload,
   or bind it to an app group with Executor
   on the target workload or its folder. For workloads using caller data mode,
   that identity also needs the relevant schema roles, directly or through a group. Workloads using a fixed
   execution identity retain that separately protected execution identity.

The outside caller authenticates to the API. The API's managed identity must then
pass the ordinary workload execution checks. Exposing an endpoint does not grant
that identity permission to run its target or to read data. There is no direct SQL or
dataset endpoint behind an internal API.

JWT configuration includes issuer, audience, JWKS URL, asymmetric algorithms and
optional required claims (such as a scope). Tokens need expiry, issuer, audience and
a nonempty subject. JWKS URLs must be reachable by the control plane. mTLS references
a shared certificate secret containing trusted issuers, with optional subject and
SHA-256 fingerprint restrictions. FDP verifies the leaf directly against an
issuer in the configured certificate/CA bundle; include the issuing intermediate in
that bundle when needed. It does not provide certificate revocation-list checking.

REST paths use literal segments and named parameters, such as
`POST /reports/{region}`. Arguments default to matching names in path, query, then
JSON body. Explicit mappings support `path.region`, `query.region`, `body.filter.region`,
`header.x-region`, or `=North` for a literal. Only declared program arguments are
passed on, and the normal argument validation still applies.

```sh
curl -X POST https://api.fdp.example.com:9443/gw/finance-reports/reports/North \
  -H "Authorization: Bearer $CALLER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2026-01-01","end_date":"2026-01-31"}'
```

For mTLS, add `--cert client.pem --key client.key`. The API's actual address is shown
in the app (`apiGateway.publicUrl` in Helm; port 8091 in local development).

Async operations return HTTP 202 with `run_id`, `status` and `status_url`, also in the
Location header. Poll that URL with the same JWT subject and/or client certificate.
Another caller cannot retrieve that run through this API. Sync operations wait up to
their configured timeout (maximum 120 seconds), then return 202 if still running, the
program's chosen status code once it finished, or 502 if it failed or was stopped.

The program answers with `fdp.respond(data, status=200)` (Python) or
`fdp::respond(data, status = 200)` (R). `data` becomes `result` (anything JSON-serialisable;
data frames become a list of rows, at most 10 MB) and `status` the HTTP status code, so a
program can answer with an error, for example:

```python
import fdp
region = fdp.args()["region"]
if region not in ("North", "South"):
    fdp.respond({"error": f"unknown region {region}"}, status=404)
else:
    fdp.respond({"region": region, "orders": 42})
```

Every finished answer includes `status_code`: sync callers get it as the HTTP status too;
the status URL always answers 200 (the poll itself succeeded) with `status_code` in the body.
A run that failed or was stopped has `status_code` 502. Without `fdp.respond`, a program may
write `response.json` (and optionally `response.status`) in `FDP_OUTPUT_DIR` itself. The API
does not automatically return arbitrary run files, logs or source code.

SOAP selects the operation using SOAPAction (1.1), the content-type action parameter
(1.2), or the operation element name. Child elements supply arguments. Responses
use SOAP envelopes and faults. FDP does not generate WSDL or import an
OpenAPI/WSDL definition. Request bodies are limited to 10 MB; SOAP parsing to 1 MB.

## External: programs call registered REST/SOAP backends

Create an External API with the backend's base URL, then declare its operations.
Grant the calling program/service identity Consumer on the API, group or operation.
Undeclared paths and methods are refused. Calls are authorized as the program's
current identity; configuration does not impersonate the person who created the API.

Configuration supports literal and secret-backed headers/query parameters, API keys,
Basic, bearer tokens, OAuth2 client credentials, client certificates, trusted server
CAs, timeouts and request/response header allowlists. Only shared `group:<name>/<secret>`
and `sa:<account>/<secret>` references are accepted, and the person configuring them
must be allowed to use those secrets. Credentials stay on the control plane. A Consumer
grant delegates use of the configured credentials for the declared operation; callers
do not need separate membership of the group storing the credential.

Configured headers/query values override caller values with the same name. Incoming
Authorization, cookies, gateway proof headers and transport headers are never passed
through. Backend redirects are returned rather than followed. Decoded response bodies
are limited to 10 MB; transport/compression headers are rebuilt for the returned body.
Backend TLS verification is on by default. Skipping verification is an explicit test
setting. The control plane needs network access to the configured backend and token URL.

```python
import fdp

response = fdp.api("crm").get("/customers/7", params={"expand": "orders"})
response.raise_for_status()
customer = response.json()

soap = fdp.api("bank").soap("GetBalance", "<GetBalance><id>7</id></GetBalance>")
```

```r
response <- fdp::api_call("crm", "GET", "/customers/7")
httr2::resp_status(response)
```

SOAP callers use the operation name as the SDK path; Floren Data Platform forwards its declared
backend path, wraps a body element if necessary, and sets the SOAP action/version.
The API helpers return HTTP responses, so callers should check status codes.

## Permissions and audit

Permissions inherit additively: APIs root → API → endpoint group → operation. Grants
at a child add access; they do not block inherited permissions.

| Role | Permissions |
|---|---|
| Consumer | View and call permitted operations |
| Contributor | Consumer plus edit configuration/operations |
| Admin | Contributor plus create/delete |
| Owner | Admin plus permission assignment and identity management |

Platform admins create APIs. Owners assign roles directly to service identities or through app groups,
which bind IdP users/groups or app-managed identities. A grant only on one operation
shows its API and containing group for navigation, without exposing sibling operations
or their call records. External Consumer checks apply to Floren Data Platform identities; outside
callers of Internal APIs are governed by that API's configured JWT/mTLS requirements.

Configuration changes, denied calls and outbound calls are audited. The Calls tab
shows the latest 100 internal call records with caller, HTTP status, run ID and run
state, restricted to readable operations. It does not store request/response bodies.
API access does not grant access to the managed identity's general run logs or files.

## Gateway deployment

Compose exposes the app on 8090 and incoming program APIs on 8091. A separate TLS entry
point requests client certificates while allowing JWT-only APIs. Each API verifies its
own trust policy. The gateway overwrites the proof header and supplies client-certificate
information from the TLS connection. Directly supplied certificate headers are ignored.

`GET /internal/traefik/config` contains the forwarding proof and is protected by
`FDP_GATEWAY_CONFIG_TOKEN`, shared only by gateway and control plane. It is not
a user/execution credential. Compose supplies an explicit development-only value; set
a random value in deployment. The endpoint fails closed when no token is configured.
Do not expose the Traefik dashboard/API or routing configuration to workloads.

The Helm chart wires the separate `apigateway` entry point (9443), its `RequestClientCert`
TLS option and authenticated routing discovery. Add `FDP_GATEWAY_CONFIG_TOKEN`
to the control-plane Secret before upgrading. `apiGateway.publicUrl` must match the
external API hostname and port. Keep `websecure` the default entry point so app/editor
routes do not appear on the API gateway. An L4 load balancer must preserve the client TLS
connection to Traefik for mTLS.

For production server certificates, mount a Secret containing `tls.crt` and `tls.key`
and configure the file provider (certificate SANs must cover both public hostnames):

```yaml
publicUrl: https://fdp.example.com
apiGateway:
  publicUrl: https://api.fdp.example.com:9443
traefik:
  volumes:
    - { name: fdp-gateway-tls, mountPath: /certs, type: secret }
  providers:
    file:
      enabled: true
      content: |
        tls:
          stores:
            default:
              defaultCertificate:
                certFile: /certs/tls.crt
                keyFile: /certs/tls.key
          options:
            api-gateway:
              clientAuth:
                clientAuthType: RequestClientCert
```

If `existingSecret` is renamed, also update the gateway token's Secret reference in
`traefik.env`, preserving POD_NAMESPACE. When using an existing Traefik installation
(`traefik.enabled: false`), apply equivalent HTTP-provider authentication, file TLS
options, entry points and routing there. The backend references `api-gateway@file`.
See the upstream [HTTP provider](https://doc.traefik.io/traefik/v3.1/providers/http/) and
[TLS options](https://doc.traefik.io/traefik/v3.3/https/tls/) configuration references.

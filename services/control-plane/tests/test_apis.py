"""API management: internal APIs run deployed programs as their managed identity; external APIs are called
by programs as themselves, with credentials kept server-side; every level has its own permissions."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import apis as core
from app import deps
from .conftest import as_
from .rbac_helpers import grant_group
from .test_platform import key_pem, make_certificate
from .test_deploying import deploy, repo  # noqa: F401


def internal_body(**inbound):
    return {"name": "Orders API", "slug": "orders", "direction": "internal", "protocol": "rest", "inbound": inbound}


@pytest.fixture
def issuer(monkeypatch):
    """A JWT issuer whose signing key the gateway resolves through its JWKS URL."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(core, "_jwks_client", lambda uri: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())))

    def token(aud="orders-api", sub="partner-1", **claims):
        return pyjwt.encode({"iss": "https://idp.partner.test", "aud": aud, "sub": sub, "exp": int(time.time()) + 300, **claims},
                            key, algorithm="RS256")
    return token


JWT = {"jwt": {"enabled": True, "issuer": "https://idp.partner.test", "audience": "orders-api",
               "jwks_uri": "https://idp.partner.test/jwks", "required_claims": {"scope": "orders.run"}}}


def test_api_management_permissions_and_validation(client):
    client.get("/v1/me", headers=as_("bob"))
    assert client.post("/v1/apis", headers=as_("bob"), json=internal_body(**JWT)).status_code == 403
    r = client.post("/v1/apis", headers=as_("admin"), json=internal_body())
    assert r.status_code == 422 and "anonymous" in r.json()["detail"]["errors"]["inbound.auth"]
    client.put("/v1/secrets/user/me/ca", headers=as_("admin"), json={"value": "x"})
    r = client.post("/v1/apis", headers=as_("admin"), json=internal_body(mtls={"enabled": True, "ca_secret": "me/ca"}))
    assert "personal secrets cannot be shared" in r.json()["detail"]["errors"]["inbound.mtls.ca_secret"]

    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(**JWT))
    assert api.status_code == 201, api.text
    api = api.json()
    assert api["endpoint"].endswith("/gw/orders") and api["identity"]["name"] == "API: Orders API"
    assert {"assign", "edit", "create", "delete", "call"} <= set(api["actions"])
    # Readers see nothing until granted; access to one endpoint group shows the API with only that group.
    assert client.get("/v1/apis", headers=as_("bob")).json()["apis"] == []
    assert client.post("/v1/apis", headers=as_("admin"), json=internal_body(**JWT)).json()["detail"]["errors"]["slug"]


def test_internal_rest_endpoint_runs_the_program_as_the_api_identity(client, repo, issuer):  # noqa: F811
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(**JWT)).json()
    op = client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
        "name": "explore-region", "group": "Exploration", "method": "POST", "path": "/regions/{region}/explore",
        "internal": {"workload_id": pub["id"], "args": {}}})
    assert op.status_code == 201, op.text
    assert client.get(f"/v1/apis/{api['id']}", headers=as_("admin")).json()["groups"][0]["name"] == "Exploration"
    url = "/gw/orders/regions/South/explore"
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"Authorization": f"Bearer {issuer(aud='other')}"}).status_code == 401
    assert client.post(url, headers={"Authorization": f"Bearer {issuer()}"}).status_code == 403   # no orders.run scope
    good = {"Authorization": f"Bearer {issuer(scope='orders.read orders.run')}"}
    r = client.post(url, headers=good)
    assert r.status_code == 403 and "managed identity may not run" in r.json()["error"]

    # The API's managed identity can receive Executor directly, without an app group.
    response = client.put(f"/v1/rbac/resources/{pub['resource_id']}/assignments", headers=as_("admin"),
                          json={"principal_id": api["identity"]["principal_id"], "role": "Executor"})
    assert response.status_code == 200, response.text
    r = client.post(url, headers=good)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status_url"].endswith(f"/gw/orders/_runs/{run['run_id']}") and r.headers["location"] == run["status_url"]
    assert f"run-{run['run_id']}" in deps.get_orchestrator().executions
    from app.db import SessionLocal
    from app.models import Run
    with SessionLocal() as db:
        stored = db.get(Run, run["run_id"])
        assert stored.owner == f"service:{api['identity']['principal_id']}" and stored.args == {"region": "South"}
    # Status is visible to the same caller only.
    assert client.get(f"/gw/orders/_runs/{run['run_id']}", headers=good).json()["run_id"] == run["run_id"]
    other = {"Authorization": f"Bearer {issuer(sub='partner-2', scope='orders.run')}"}
    assert client.get(f"/gw/orders/_runs/{run['run_id']}", headers=other).status_code == 404
    calls = client.get(f"/v1/apis/{api['id']}/calls", headers=as_("admin")).json()
    assert calls[0]["caller"] == "jwt:partner-1" and calls[0]["operation"] == "explore-region"


def test_internal_mtls_trusts_only_gateway_forwarded_certificates_from_the_configured_ca(client, repo):  # noqa: F811
    from urllib.parse import quote
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    ca, ca_key, ca_pem = make_certificate("Partner CA")
    leaf, _, leaf_pem = make_certificate("partner-client", ca_key, ca.subject)
    _, _, rogue_pem = make_certificate("rogue-client")
    assert client.put("/v1/secrets/group/finance/partner-ca", headers=as_("ana"),
                      json={"kind": "certificate", "certificate": ca_pem}).status_code == 200
    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(mtls={"enabled": True, "ca_secret": "group:finance/partner-ca"}))
    assert api.status_code == 201, api.text
    api = api.json()
    client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
        "name": "explore", "method": "POST", "path": "/explore", "internal": {"workload_id": pub["id"], "args": {"region": "query.r"}}})
    grant_group(client, pub["resource_id"], "Orders API runners", "Executor", services=[api["identity"]["principal_id"]])
    cert = lambda pem: quote(pem.replace("-----BEGIN CERTIFICATE-----", "").replace("-----END CERTIFICATE-----", "").replace("\n", ""))
    proof = core.gateway_proof()
    # A certificate header is ignored unless the gateway forwarded it.
    assert client.post("/gw/orders/explore?r=East", headers={"X-Forwarded-Tls-Client-Cert": cert(leaf_pem)}).status_code == 401
    assert client.post("/gw/orders/explore?r=East", headers={"X-Forwarded-Tls-Client-Cert": cert(rogue_pem),
                                                            "X-FDP-Gateway-Proof": proof}).status_code == 401
    r = client.post("/gw/orders/explore?r=East", headers={"X-Forwarded-Tls-Client-Cert": cert(leaf_pem), "X-FDP-Gateway-Proof": proof})
    assert r.status_code == 202, r.text
    from app.db import SessionLocal
    from app.models import Run
    with SessionLocal() as db:
        assert db.get(Run, r.json()["run_id"]).args == {"region": "East"}


def test_internal_soap_endpoint(client, repo, issuer):  # noqa: F811
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    api = client.post("/v1/apis", headers=as_("admin"), json={**internal_body(**JWT), "slug": "orders-soap", "protocol": "soap"}).json()
    client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
        "name": "Explore", "method": "POST", "path": "/", "soap_action": "urn:orders/Explore", "internal": {"workload_id": pub["id"]}})
    grant_group(client, pub["resource_id"], "Orders API runners", "Executor", services=[api["identity"]["principal_id"]])
    envelope = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
                '<Explore xmlns="urn:orders"><region>West</region></Explore></soap:Body></soap:Envelope>')
    headers = {"Authorization": f"Bearer {issuer(scope='orders.run')}", "SOAPAction": '"urn:orders/Explore"', "Content-Type": "text/xml"}
    r = client.post("/gw/orders-soap", headers=headers, content=envelope)
    assert r.status_code == 202 and "<ExploreResponse><runId>" in r.text and "text/xml" in r.headers["content-type"]
    bomb = '<!DOCTYPE x [<!ENTITY a "aaaa">]>' + envelope
    r = client.post("/gw/orders-soap", headers=headers, content=bomb)
    assert r.status_code == 500 and "<faultcode>soap:Client</faultcode>" in r.text and "DTDs" in r.text


class Backend(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Backend.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        body = json.dumps({"id": 7, "name": "Acme"}).encode()
        compressed = self.path.endswith('/compressed')
        if compressed:
            import gzip
            body = gzip.compress(body)
        self.send_response(200)
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
        for k, v in (("Content-Type", "application/json"), ("X-Request-Id", "r-1"), ("Set-Cookie", "session=leak"), ("Content-Length", str(len(body)))):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Backend.seen.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()},
                             'body': self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()})
        body = b'<BalanceResponse>7</BalanceResponse>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def backend():
    Backend.seen = []
    server = HTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_external_api_calls_use_configured_secrets_and_consumer_permissions(client, backend):
    client.get("/v1/me", headers=as_("bob"))
    assert client.put("/v1/secrets/group/finance/crm-token", headers=as_("ana"), json={"value": "crm-bearer-123"}).status_code == 200
    assert client.put("/v1/secrets/group/finance/crm-tenant", headers=as_("ana"), json={"value": "tenant-42"}).status_code == 200
    # Configuring with a secret requires being able to use it.
    body = {"name": "CRM", "slug": "crm", "direction": "external", "protocol": "rest", "outbound": {
        "base_url": backend + "/api", "auth": {"type": "bearer", "secret": "group:finance/crm-token"},
        "headers": [{"name": "X-Tenant", "secret": "group:finance/crm-tenant"}, {"name": "X-Source", "value": "fdp"}],
        "pass_request_headers": ["X-Correlation-Id"], "pass_response_headers": ["content-type", "x-request-id"]}}
    assert client.post("/v1/apis", headers=as_("admin"), json={**body, "outbound": {**body["outbound"], "pass_request_headers": ["Authorization"]}}).status_code == 422
    client.get("/v1/rbac/resources", headers=as_("admin"))   # creates the APIs root
    grant_group(client, "apis", "API builders", "Admin", users=("ana",))
    api = client.post("/v1/apis", headers=as_("ana"), json=body)
    assert api.status_code == 201, api.text
    api = api.json()
    op = client.post(f"/v1/apis/{api['id']}/operations", headers=as_("ana"), json={
        "name": "get-customer", "group": "Customers", "method": "GET", "path": "/customers/{id}"}).json()
    # bob may not call it until an Owner grants Consumer on the operation (or its group or API).
    assert client.get("/v1/egress/crm/customers/7", headers=as_("bob")).status_code == 404
    grant_group(client, op["resource_id"], "CRM consumers", "Consumer", users=("bob",))
    r = client.get("/v1/egress/crm/customers/7?expand=orders", headers={**as_("bob"), "X-Correlation-Id": "c-9", "Cookie": "x=1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": 7, "name": "Acme"} and r.headers["x-request-id"] == "r-1" and "set-cookie" not in r.headers
    seen = Backend.seen[-1]
    assert seen["path"] == "/api/customers/7?expand=orders"
    assert seen["headers"]["authorization"] == "Bearer crm-bearer-123" and seen["headers"]["x-tenant"] == "tenant-42"
    assert seen["headers"]["x-correlation-id"] == "c-9" and "cookie" not in seen["headers"]
    # Undeclared paths are refused; the program never sees the credentials.
    assert client.get("/v1/egress/crm/admin/users", headers=as_("bob")).status_code == 404
    assert "crm-bearer" not in json.dumps(client.get(f"/v1/apis/{api['id']}", headers=as_("bob")).json())


def tls_material(tmp_path):
    """A private CA, a server certificate for 127.0.0.1 and a client certificate, all from that CA."""
    import ipaddress
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    ca, ca_key, ca_pem = make_certificate("Backend CA")
    now = datetime.now(timezone.utc)

    def issue(cn, san):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
                .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName(san), critical=False).sign(ca_key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem(key)
    server = issue("127.0.0.1", [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    client_cert = issue("fdp-client", [x509.DNSName("fdp-client")])
    (tmp_path / "ca.pem").write_text(ca_pem)
    (tmp_path / "server.pem").write_text(server[0])
    (tmp_path / "server.key").write_text(server[1])
    return ca_pem, client_cert


def test_external_mtls_presents_the_client_certificate_and_trusts_the_configured_ca(client, tmp_path):
    import ssl
    ca_pem, (client_pem, client_key) = tls_material(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    context.load_verify_locations(tmp_path / "ca.pem")
    context.verify_mode = ssl.CERT_REQUIRED          # the backend refuses callers without a client certificate
    Backend.seen = []
    server = HTTPServer(("127.0.0.1", 0), Backend)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        put = lambda name, body: client.put(f"/v1/secrets/group/finance/{name}", headers=as_("ana"), json={"kind": "certificate", **body})
        assert put("bank-client", {"certificate": client_pem, "private_key": client_key}).status_code == 200
        assert put("bank-ca", {"certificate": ca_pem}).status_code == 200
        outbound = {"base_url": f"https://127.0.0.1:{server.server_port}", "client_certificate": "group:finance/bank-ca",
                    "server_ca": "group:finance/bank-ca"}
        body = {"name": "Bank", "slug": "bank", "direction": "external", "outbound": outbound}
        r = client.post("/v1/apis", headers=as_("admin"), json=body)
        assert r.status_code == 422 and "no private key" in r.json()["detail"]["errors"]["outbound.client_certificate"]
        api = client.post("/v1/apis", headers=as_("admin"), json={**body, "outbound": {**outbound, "client_certificate": "group:finance/bank-client"}})
        assert api.status_code == 201, api.text
        op = client.post(f"/v1/apis/{api.json()['id']}/operations", headers=as_("admin"), json={"name": "balance", "method": "GET", "path": "/balance"}).json()
        grant_group(client, op["resource_id"], "Bank consumers", "Consumer", users=("ana",))
        r = client.get("/v1/egress/bank/balance", headers=as_("ana"))
        assert r.status_code == 200, r.text
        assert Backend.seen[-1]["path"] == "/balance"
    finally:
        server.shutdown()


def test_internal_apis_have_their_own_gateway_address_that_asks_for_client_certificates(client):
    from app.config import get_settings
    http = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]
    gw = http["routers"]["gw"]
    # TLS options follow the host name, so the router names the API gateway host on its own entry point.
    assert gw["rule"] == "Host(`localhost`) && PathPrefix(`/gw/`)" and gw["entryPoints"] == [get_settings().api_gateway_entrypoint]
    assert gw["tls"] == {"options": "api-gateway@file"} and gw["middlewares"] == ["gw-client-cert", "gw-proof"]
    assert http["middlewares"]["gw-proof"]["headers"]["customRequestHeaders"]["X-FDP-Gateway-Proof"] == core.gateway_proof()
    assert http["middlewares"]["gw-client-cert"] == {"passTLSClientCert": {"pem": True}}


def test_gateway_proof_configuration_is_not_available_to_workloads(client, monkeypatch):
    from app.config import get_settings
    endpoint = "/internal/traefik/config"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=as_("admin")).status_code == 401
    assert client.get(endpoint, headers={"X-FDP-Gateway-Config": "wrong"}).status_code == 401
    response = client.get(endpoint, headers={"X-FDP-Gateway-Config": "test-gateway-config-token"})
    assert response.status_code == 200 and core.gateway_proof() in response.text
    monkeypatch.setattr(get_settings(), "gateway_config_token", "")
    assert client.get(endpoint).status_code == 503


def test_operation_grants_allow_scoped_editing_without_sibling_metadata_or_calls(client):
    from app.db import SessionLocal
    from app.models import ApiCall
    api = client.post("/v1/apis", headers=as_("admin"), json={"name": "CRM", "slug": "crm", "direction": "external",
        "outbound": {"base_url": "https://crm.test"}}).json()
    endpoint = f"/v1/apis/{api['id']}"
    def operation(name, group):
        r = client.post(endpoint + '/operations', headers=as_('admin'), json={'name': name, 'group': group, 'path': '/' + name})
        assert r.status_code == 201, r.text
        return r.json()
    one, two = operation('permitted', 'Finance'), operation('hidden', 'Other department')
    grant_group(client, one['resource_id'], 'Scoped API editors', 'Contributor', users=['bob'])
    with SessionLocal() as db:
        db.add_all([ApiCall(api_id=api['id'], operation_id=one['id'], caller='visible-caller', status_code=200),
                    ApiCall(api_id=api['id'], operation_id=two['id'], caller='hidden-caller', status_code=200)])
        db.commit()
    listing = client.get('/v1/apis', headers=as_('bob')).json()['apis']
    assert listing[0]['operation_count'] == 1
    details = client.get(endpoint, headers=as_('bob')).json()
    assert [g['name'] for g in details['groups']] == ['Finance']
    assert [o['name'] for o in details['operations']] == ['permitted']
    calls = client.get(endpoint + '/calls', headers=as_('bob'))
    assert calls.status_code == 200 and [c['caller'] for c in calls.json()] == ['visible-caller']
    r = client.put(endpoint + '/operations/' + one['id'], headers=as_('bob'), json={
        'name': 'permitted', 'group': 'Finance', 'path': '/permitted', 'description': 'Updated by scoped editor'})
    assert r.status_code == 200, r.text
    assert client.put(endpoint + '/operations/' + two['id'], headers=as_('bob'), json={
        'name': 'hidden', 'group': 'Other department', 'path': '/hidden'}).status_code == 403
    assert client.put(endpoint + '/operations/' + one['id'], headers=as_('bob'), json={
        'name': 'permitted', 'group': 'Other department', 'path': '/permitted'}).status_code == 403


def test_outbound_paths_and_configured_credentials_cannot_be_overridden(client, backend):
    client.put('/v1/secrets/group/finance/api-key', headers=as_('ana'), json={'value': 'configured-key'})
    body = {'name': 'Partner', 'slug': 'partner', 'direction': 'external', 'outbound': {
        'base_url': backend + '/api', 'headers': [{'name': 'X-Tenant', 'value': 'configured-tenant'}],
        'query': [{'name': 'tenant', 'value': 'finance'}], 'pass_request_headers': ['X-Tenant'],
        'auth': {'type': 'api_key', 'name': 'api_key', 'location': 'query', 'secret': 'group:finance/api-key'}}}
    api = client.post('/v1/apis', headers=as_('admin'), json=body).json()
    endpoint = f"/v1/apis/{api['id']}/operations"
    for path in ['/customers/{id}/{id}', '/customers/..', '/' + 'x' * 257]:
        assert client.post(endpoint, headers=as_('admin'), json={'name': 'invalid', 'path': path}).status_code == 422
    op = client.post(endpoint, headers=as_('admin'), json={'name': 'customer', 'path': '/customers/{id}'}).json()
    grant_group(client, op['resource_id'], 'Partner consumers', 'Consumer', users=['bob'])
    r = client.get('/v1/egress/partner/customers/7?api_key=attacker&tenant=wrong', headers={**as_('bob'), 'x-tenant': 'wrong'})
    assert r.status_code == 200, r.text
    assert Backend.seen[-1]['path'] == '/api/customers/7?tenant=finance&api_key=configured-key'
    assert Backend.seen[-1]['headers']['x-tenant'] == 'configured-tenant'
    count = len(Backend.seen)
    for segment in ['%252e%252e', '%252fadmin', '%255cadmin']:
        assert client.get('/v1/egress/partner/customers/' + segment, headers=as_('bob')).status_code in (400, 404)
    assert len(Backend.seen) == count
    for url in [backend + '?key=secret', 'http://user:password@backend.test', 'https://backend.test/#fragment']:
        invalid = client.put(f"/v1/apis/{api['id']}", headers=as_('admin'), json={**body, 'outbound': {**body['outbound'], 'base_url': url}})
        assert invalid.status_code == 422


def test_missing_caller_subject_and_empty_certificates_are_rejected(client, issuer):
    client.post('/v1/apis', headers=as_('admin'), json=internal_body(**JWT))
    for subject in ['', 'x' * 513]:
        r = client.post('/gw/orders/anything', headers={'Authorization': 'Bearer ' + issuer(sub=subject, scope='orders.run')})
        assert r.status_code == 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        core.verify_client_certificate({}, ',', '')
    assert exc.value.status_code == 401


def test_external_soap_action_and_decoded_http_responses(client, backend):
    soap = client.post('/v1/apis', headers=as_('admin'), json={'name': 'Bank', 'slug': 'bank', 'direction': 'external',
        'protocol': 'soap', 'outbound': {'base_url': backend}}).json()
    op = client.post(f"/v1/apis/{soap['id']}/operations", headers=as_('admin'), json={
        'name': 'Balance', 'method': 'POST', 'path': '/soap', 'soap_action': 'urn:Balance'}).json()
    grant_group(client, op['resource_id'], 'SOAP consumers', 'Consumer', users=['bob'])
    response = client.post('/v1/egress/bank/Balance', headers=as_('bob'), content='<Balance><id>7</id></Balance>')
    assert response.status_code == 200 and response.text == '<BalanceResponse>7</BalanceResponse>'
    assert Backend.seen[-1]['path'] == '/soap'
    assert Backend.seen[-1]['headers']['soapaction'] == '"urn:Balance"'
    assert '<soap:Envelope' in Backend.seen[-1]['body']
    assert client.get('/v1/egress/bank/Balance', headers=as_('bob')).status_code == 405
    rest = client.post('/v1/apis', headers=as_('admin'), json={'name': 'Compressed', 'slug': 'compressed', 'direction': 'external',
        'outbound': {'base_url': backend, 'pass_response_headers': ['content-type', 'content-encoding', 'content-length']}}).json()
    op = client.post(f"/v1/apis/{rest['id']}/operations", headers=as_('admin'), json={'name': 'compressed', 'path': '/compressed'}).json()
    grant_group(client, op['resource_id'], 'Compressed consumers', 'Consumer', users=['bob'])
    response = client.get('/v1/egress/compressed/compressed', headers=as_('bob'))
    assert response.status_code == 200 and response.json()['name'] == 'Acme'
    assert 'content-encoding' not in response.headers
    assert int(response.headers['content-length']) == len(response.content)


def test_anonymous_internal_apis_are_for_testing_outside_production(client, repo):  # noqa: F811
    from app import layers
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    mixed = client.post("/v1/apis", headers=as_("admin"), json=internal_body(anonymous=True, **JWT))
    assert mixed.status_code == 422 and "turn those off" in mixed.json()["detail"]["errors"]["inbound.auth"]
    in_prod = client.post("/v1/apis", headers=as_("admin"), json={**internal_body(anonymous=True), "layer_id": layers.PROD})
    assert in_prod.status_code == 422 and "production layer" in in_prod.json()["detail"]["errors"]["inbound.auth"]
    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(anonymous=True))
    assert api.status_code == 201, api.text
    api = api.json()
    client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
        "name": "explore", "method": "POST", "path": "/explore", "internal": {"workload_id": pub["id"], "args": {}}}).raise_for_status()
    client.put(f"/v1/rbac/resources/{pub['resource_id']}/assignments", headers=as_("admin"),
               json={"principal_id": api["identity"]["principal_id"], "role": "Executor"}).raise_for_status()
    r = client.post("/gw/orders/explore")  # no token, no certificate
    assert r.status_code == 202, r.text
    calls = client.get(f"/v1/apis/{api['id']}/calls", headers=as_("admin")).json()
    assert [c["caller"] for c in (calls.get("calls") if isinstance(calls, dict) else calls)] == ["anonymous"]
    audit = client.get("/v1/admin/audit", headers=as_("admin")).json()
    assert any(e["action"] == "api.anonymous_enabled" for e in audit)


def test_a_program_answers_internal_api_calls_with_its_own_status_code(client, repo):  # noqa: F811
    import os
    from app.config import get_settings
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(anonymous=True)).json()
    for name, mode in (("lookup", "sync"), ("later", "async")):
        client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
            "name": name, "method": "POST", "path": f"/{name}", "internal": {"workload_id": pub["id"], "mode": mode, "timeout_seconds": 5, "args": {}}
        }).raise_for_status()
    client.put(f"/v1/rbac/resources/{pub['resource_id']}/assignments", headers=as_("admin"),
               json={"principal_id": api["identity"]["principal_id"], "role": "Executor"}).raise_for_status()
    answer = {"body": {"error": "unknown region"}, "status": "404", "outcome": "succeeded"}

    def program(spec):  # what fdp.respond(...) leaves in the run's outputs
        folder = os.path.join(get_settings().data_dir, "runs", spec.name[len("run-"):], "outputs")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "response.json"), "w").write(json.dumps(answer["body"]))
        open(os.path.join(folder, "response.status"), "w").write(answer["status"])
        return answer["outcome"]
    deps.get_orchestrator().on_start = program

    r = client.post("/gw/orders/lookup")
    assert r.status_code == 404 and r.json()["status"] == "succeeded"
    assert (r.json()["status_code"], r.json()["result"]) == (404, {"error": "unknown region"})

    answer.update(body={"orders": 42}, status="200")
    r = client.post("/gw/orders/lookup")
    assert r.status_code == 200 and r.json()["status_code"] == 200 and r.json()["result"] == {"orders": 42}

    answer.update(body={"error": "invalid date"}, status="422")
    started = client.post("/gw/orders/later")
    assert started.status_code == 202
    polled = client.get(f"/gw/orders/_runs/{started.json()['run_id']}")
    assert polled.status_code == 200 and (polled.json()["status_code"], polled.json()["result"]) == (422, {"error": "invalid date"})

    answer.update(outcome="failed")
    crashed = client.post("/gw/orders/lookup")
    assert crashed.status_code == 502 and crashed.json()["status_code"] == 502

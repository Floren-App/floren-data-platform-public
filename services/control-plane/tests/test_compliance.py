"""Access policies: forms, the policy grid, review steps, enforcement on every way of giving access, memberships."""
from sqlalchemy import select

from app import layers, rbac
from app.db import SessionLocal
from app.models import AppGroup, JustifiedGrant, Resource, RoleAssignment
from .conftest import USERS, as_
from .rbac_helpers import grant_group
from .test_classification_options import classify
from .test_deploying import repo  # noqa: F401
from .test_products import setup_products

FORM = {"pages": [
    {"id": "purpose", "title": "Purpose", "fields": [
        {"id": "purpose", "label": "Purpose of use", "type": "long_text", "required": True},
        {"id": "data", "label": "Personal data you will see", "type": "multi_choice", "required": True, "options": ["email", "health"]}]},
    {"id": "special", "title": "Special categories", "show_if": {"field": "data", "contains": "health"}, "fields": [
        {"id": "basis", "label": "Legal basis for health data", "type": "short_text", "required": True}]},
    {"id": "safeguards", "title": "Safeguards", "fields": [
        {"id": "aggregate", "label": "Results leave the platform only aggregated", "type": "statement", "required": True}]}]}
ANSWERS = {"purpose": "Monthly report", "data": ["email"], "aggregate": True}


def can_read(user, resource_id):
    with SessionLocal() as db:
        return rbac.allowed(db, rbac.resolve_user(db, USERS[user]), "read", db.get(Resource, resource_id))


def set_up_policy(client, schema_id, steps=None):
    with SessionLocal() as db:
        layer = layers.info(db, db.get(Resource, schema_id))["layer_id"]
    form = client.put("/v1/compliance/forms/Privacy impact", headers=as_("admin"), json={"name": "Privacy impact", "definition": FORM})
    assert form.status_code == 200, form.text
    assert client.post(f"/v1/compliance/forms/{form.json()['id']}/publish", headers=as_("admin")).status_code == 200
    policy = client.put(f"/v1/compliance/policies/{layer}/restricted", headers=as_("admin"), json={
        "form_id": form.json()["id"], "steps": steps or [{"role": "compliance_reviewer"}], "max_days": 30, "review_days": 10})
    assert policy.status_code == 200, policy.text
    return layer, form.json()["id"]


def test_forms_have_pages_conditions_and_versions(client):
    assert client.put("/v1/compliance/forms/x", headers=as_("ana"), json={"name": "x", "definition": FORM}).status_code == 403
    bad = client.put("/v1/compliance/forms/x", headers=as_("admin"), json={"name": "x", "definition": {"pages": [{"fields": [
        {"id": "a", "type": "choice", "label": "A"}]}]}})
    assert bad.status_code == 422 and "options" in bad.text
    later = client.put("/v1/compliance/forms/x", headers=as_("admin"), json={"name": "x", "definition": {"pages": [{"fields": [
        {"id": "a", "type": "short_text", "show_if": {"field": "b", "equals": "y"}}]}]}})
    assert later.status_code == 422 and "earlier field" in later.text
    form = client.put("/v1/compliance/forms/Privacy impact", headers=as_("admin"), json={"name": "Privacy impact", "definition": FORM}).json()
    assert form["published_version"] is None and form["has_draft"]
    form = client.post(f"/v1/compliance/forms/{form['id']}/publish", headers=as_("admin")).json()
    assert form["published_version"] == 1 and not form["has_draft"]
    # Editing a published form makes a new draft; version 1 stays for the cases filled in with it.
    edited = client.put("/v1/compliance/forms/Privacy impact", headers=as_("admin"), json={"name": "Privacy impact", "definition": {
        "pages": FORM["pages"] + [{"id": "extra", "title": "Extra", "fields": [{"id": "dpia", "label": "DPIA", "type": "file"}]}]}}).json()
    assert [v["version"] for v in edited["versions"]] == [1, 2] and edited["published_version"] == 1 and edited["has_draft"]


def test_role_grants_need_an_approved_case_reviewed_by_someone_else(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    layer, _ = set_up_policy(client, ids["public"])
    marketing = grant_group(client, "workloads", "Marketing readers", "Executor")   # no members yet
    privacy = grant_group(client, "workloads", "Privacy team", "Executor", users=["bob"])
    assert client.post("/v1/compliance/roles", headers=as_("ana"), json={"role": "compliance_reviewer", "app_group_id": privacy}).status_code == 403
    role = client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_reviewer", "app_group_id": privacy,
                                                                             "scope": {"layers": [layer], "classifications": ["restricted"]}})
    assert role.status_code == 201, role.text
    # The schema holds restricted data in this layer: a Reader grant needs a justification.
    refused = client.put(f"/v1/rbac/resources/{ids['public']}/assignments", headers=as_("ana"), json={"app_group_id": marketing, "role": "Reader"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "justification_required"
    assert refused.json()["detail"]["requirement"]["form"]["name"] == "Privacy impact"
    need = client.post("/v1/compliance/requirement", headers=as_("ana"), json={"kind": "role", "resource_id": ids["public"], "role": "Reader",
                                                                               "app_group_id": marketing}).json()
    assert need["required"] and need["classification"] == "restricted" and need["steps"] == [{"role": "compliance_reviewer", "approvals": 1}]
    # Required answers of visible pages; a page shown by a condition counts only when shown.
    target = {"kind": "role", "resource_id": ids["public"], "role": "Reader", "app_group_id": marketing}
    assert client.post("/v1/compliance/cases", headers=as_("ana"), json={**target, "answers": {"purpose": "x", "data": ["email"]}}).status_code == 422
    assert client.post("/v1/compliance/cases", headers=as_("ana"), json={**target, "answers": {**ANSWERS, "data": ["health"]}}).status_code == 422
    case = client.post("/v1/compliance/cases", headers=as_("ana"), json={**target, "answers": ANSWERS})
    assert case.status_code == 201, case.text
    case = case.json()
    assert case["status"] == "in_review" and case["due_at"] and case["form"] == "Privacy impact" and case["form_version"] == 1
    # Nobody reviews their own case; reviewers in scope see it waiting.
    assert client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("ana"), json={"decision": "approve"}).status_code == 403
    assert [c["id"] for c in client.get("/v1/compliance/cases", headers=as_("bob"), params={"box": "review"}).json()] == [case["id"]]
    # Asking for changes sends it back; the requester answers and sends it again.
    asked = client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "changes"})
    assert asked.status_code == 422  # say why
    asked = client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "changes", "note": "Which report?"})
    assert asked.json()["status"] == "changes_requested"
    client.patch(f"/v1/compliance/cases/{case['id']}", headers=as_("ana"), json={"answers": {**ANSWERS, "purpose": "Monthly sales report for finance"}}).raise_for_status()
    assert client.post(f"/v1/compliance/cases/{case['id']}/submit", headers=as_("ana")).json()["status"] == "in_review"
    approved = client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "approve"}).json()
    assert approved["status"] == "approved" and approved["approved_until"]
    with SessionLocal() as db:
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == ids["public"], RoleAssignment.app_group_id == marketing)).role == "Reader"
        assert db.scalar(select(JustifiedGrant).where(JustifiedGrant.app_group_id == marketing)).case_id == case["id"]
    # Terraform and the API name the approved case.
    named = client.put(f"/v1/rbac/resources/{ids['public']}/assignments", headers=as_("ana"),
                       json={"app_group_id": marketing, "role": "Reader", "justification_id": case["id"]})
    assert named.status_code == 200
    # Someone who joins later gets the justified access only after their membership is approved.
    grant_group(client, "workloads", "Marketing readers", "Executor", users=["bob"])
    assert not can_read("bob", ids["public"])
    pending = client.get("/v1/compliance/memberships", headers=as_("admin")).json()
    assert [(p["group"], p["principal"]) for p in pending] == [("Marketing readers", "bob")]
    assert client.get("/v1/compliance/memberships", headers=as_("ana")).json() == []   # not a membership approver
    with SessionLocal() as db:
        bob = rbac.resolve_user(db, USERS["bob"]).principal_id
    assert client.post(f"/v1/compliance/memberships/{marketing}/{bob}", headers=as_("admin"), json={"approve": True}).status_code == 200
    assert can_read("bob", ids["public"])


def test_product_requests_under_a_policy_carry_the_form_and_go_through_the_steps(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    set_up_policy(client, ids["public"])
    product = client.post("/v1/catalog/products", headers=as_("ana"), json={"title": "Sales", "asset_ids": [ids["sales"]], "options": [
        {"name": "Masked", "default": True, "masks": [{"table": "sales", "column": "email", "method": "hash"}]}, {"name": "Full"}]}).json()
    masked, full = (next(o["id"] for o in product["options"] if o["name"] == n) for n in ("Masked", "Full"))
    with SessionLocal() as db:
        executors = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors")).id
    url = f"/v1/catalog/products/{product['id']}/requests"
    # Masked counts as internal: no form needed.
    plain = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Report", "option_id": masked})
    assert plain.status_code == 201 and plain.json()["case_id"] is None
    client.delete(f"/v1/catalog/requests/{plain.json()['id']}", headers=as_("bob"))
    # Full is restricted: the request needs the form.
    assert client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Fraud", "option_id": full}).status_code == 422
    request = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Fraud", "option_id": full, "answers": ANSWERS})
    assert request.status_code == 201 and request.json()["case_id"] and request.json()["with_owners"]
    # Owners decide first; then the compliance reviewers (here: administrators).
    owner = client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": True}).json()
    assert owner["status"] == "pending" and owner["case_status"] == "in_review" and not owner["with_owners"]
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": True}).status_code == 409
    case_id = request.json()["case_id"]
    assert client.post(f"/v1/compliance/cases/{case_id}/decision", headers=as_("admin"), json={"decision": "approve"}).json()["status"] == "approved"
    detail = client.get(f"/v1/catalog/products/{product['id']}", headers=as_("ana")).json()
    assert [(c["group"], c["option"]) for c in detail["consumers"]] == [("Deployed executors", "Full")]
    assert next(r for r in client.get("/v1/catalog/requests", headers=as_("bob"), params={"box": "mine"}).json() if r["id"] == request.json()["id"])["status"] == "approved"
    assert can_read("bob", ids["public"])
    # Owners giving the Full option directly need an approved case for that group and option.
    with SessionLocal() as db:
        finance = db.scalar(select(AppGroup).where(AppGroup.name == "Finance data")).id
    refused = client.put(f"/v1/catalog/products/{product['id']}/consumers/{finance}", headers=as_("ana"), json={"option_id": full})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "justification_required"
    assert client.put(f"/v1/catalog/products/{product['id']}/consumers/{finance}", headers=as_("ana"), json={"option_id": masked}).status_code == 200
    # A restricted object added later reaches the existing consumer only after a new approval.
    client.patch(f"/v1/catalog/products/{product['id']}", headers=as_("ana"), json={"asset_ids": [ids["sales"], ids["raw"]]}).raise_for_status()
    classify(client, ids["raw"], {"ssn": {"classification": "restricted"}})
    with SessionLocal() as db:
        staging = db.get(Resource, db.get(__import__("app.models", fromlist=["CatalogAsset"]).CatalogAsset, ids["raw"]).resource_id).id
    assert not can_read("bob", staging)
    again = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Need raw too", "option_id": full, "answers": ANSWERS})
    assert again.status_code == 201


def test_legacy_compliance_admin_needs_explicit_page_grants(client):
    privacy = grant_group(client, "workloads", "Privacy admins", "Executor", users=["bob"])
    levels = client.get("/v1/catalog/classifications", headers=as_("bob")).json()["levels"]
    assert client.put("/v1/catalog/classifications", headers=as_("bob"), json={"levels": levels}).status_code == 403
    assert client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_admin", "app_group_id": privacy}).status_code == 201
    me = client.get("/v1/me", headers=as_("bob")).json()
    assert me["compliance_roles"] == ["compliance_admin"]
    assert client.put("/v1/catalog/classifications", headers=as_("bob"), json={"levels": levels}).status_code == 403
    assert client.put("/v1/compliance/forms/Privacy impact", headers=as_("bob"), json={"name": "Privacy impact", "definition": FORM}).status_code == 403
    grant_group(client, "set-classes", "Privacy admins", "Manager", users=["bob"])
    grant_group(client, "set-forms", "Privacy admins", "Manager", users=["bob"])
    assert client.put("/v1/catalog/classifications", headers=as_("bob"), json={"levels": levels}).status_code == 200
    assert client.put("/v1/compliance/forms/Privacy impact", headers=as_("bob"), json={"name": "Privacy impact", "definition": FORM}).status_code == 200
    # Only platform administrators make compliance administrators.
    assert client.post("/v1/compliance/roles", headers=as_("bob"), json={"role": "compliance_admin", "app_group_id": privacy}).status_code == 403


class Hook:
    """What the platform posted to the test webhook: per recipient with an email address, like a mail automation sees it."""
    sent: list = []
    raw: list = []


def capture(monkeypatch, client, events=None, secret=None, status=200):
    import json
    from app import notify
    Hook.sent, Hook.raw = [], []

    def post(url, body, headers):
        payload = json.loads(body)
        Hook.raw.append((url, payload, headers, body))
        for r in payload["recipients"]:
            if r["email"]:
                Hook.sent.append({"to": r["email"], "subject": payload["subject"], "body": payload["text"] + "\n" + (payload["link"] or ""),
                                  "event": payload["event"]})
        return status
    monkeypatch.setattr(notify, "_post", post)
    client.put("/v1/admin/webhooks/privacy", headers=as_("admin"), json={
        "name": "privacy", "url": "https://hooks.example.test/fdp", "events": events or list(notify.EVENTS),
        **({"secret": secret} if secret else {})}).raise_for_status()


def set_email(username, address):
    from app.models import DirectoryPrincipal
    with SessionLocal() as db:
        p = db.get(DirectoryPrincipal, rbac.resolve_user(db, USERS[username]).principal_id)
        p.attributes = {**(p.attributes or {}), "email": address}
        db.commit()


def test_reviews_notify_by_email_and_land_in_the_register(client, repo, monkeypatch):  # noqa: F811
    from app import notify
    capture(monkeypatch, client)
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    layer, _ = set_up_policy(client, ids["public"])
    marketing = grant_group(client, "workloads", "=Marketing", "Executor")
    privacy = grant_group(client, "workloads", "Privacy team", "Executor", users=["bob"])
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_reviewer", "app_group_id": privacy,
                                                                    "scope": {"layers": [layer]}}).raise_for_status()
    set_email("ana", "ana@example.test")
    set_email("bob", "bob@example.test")
    target = {"kind": "role", "resource_id": ids["public"], "role": "Reader", "app_group_id": marketing}
    case = client.post("/v1/compliance/cases", headers=as_("ana"), json={**target, "answers": ANSWERS}).json()
    notify.tick(SessionLocal)
    # Reviewers are told what waits and where; the answers never go out by email.
    assert [(m["to"], m["subject"]) for m in Hook.sent] == [("bob@example.test", "Review needed: Reader on public for =Marketing")]
    assert f"/#case={case['id']}" in Hook.sent[0]["body"] and "Monthly report" not in Hook.sent[0]["body"]
    client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "changes", "note": "Which report?"})
    client.post(f"/v1/compliance/cases/{case['id']}/submit", headers=as_("ana")).raise_for_status()
    client.post(f"/v1/compliance/cases/{case['id']}/comments", headers=as_("ana"), json={"text": "It is the finance one"}).raise_for_status()
    client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "approve"}).raise_for_status()
    notify.tick(SessionLocal)
    assert [(m["to"], m["subject"].split(":")[0]) for m in Hook.sent[1:]] == [
        ("ana@example.test", "Changes asked"), ("bob@example.test", "Review needed"), ("bob@example.test", "New comment"),
        ("ana@example.test", "Approved")]
    assert "Which report?" in Hook.sent[1]["body"]
    # Membership approvers hear once about someone joining.
    grant_group(client, "workloads", "=Marketing", "Executor", users=["bob"])
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "membership_approver", "app_group_id": privacy}).raise_for_status()
    grant_group(client, "workloads", "Approvers", "Executor", users=["ana"])
    approvers = client.get("/v1/rbac/groups", headers=as_("admin")).json()
    approvers = next(g["id"] for g in approvers if g["name"] == "Approvers")
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "membership_approver", "app_group_id": approvers}).raise_for_status()
    notify.tick(SessionLocal)
    notify.tick(SessionLocal)
    assert [(m["to"], m["subject"]) for m in Hook.sent[5:]] == [("ana@example.test", "Membership to approve: =Marketing")]
    # The register: what was given through a form, by whom and until when; CSV for the auditors.
    assert client.get("/v1/compliance/register", headers=as_("ana")).status_code == 403
    rows = client.get("/v1/compliance/register", headers=as_("bob")).json()
    assert [(r["group"], r["access"], r["approved_by"], r["case_id"]) for r in rows] == [("=Marketing", "Reader on public", "bob", case["id"])]
    csv = client.get("/v1/compliance/register", headers=as_("admin"), params={"format": "csv"})
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.splitlines()
    assert lines[0].startswith("group,access,kind") and lines[1].startswith("'=Marketing,Reader on public,role,restricted")


def test_webhooks_are_admin_configured_signed_filtered_and_retried(client, monkeypatch):
    import hashlib
    import hmac
    import httpx
    from app import notify
    from app.models import Notification
    body = {"name": "chat", "url": "https://hooks.example.test/chat", "events": ["review.waiting"]}
    assert client.put("/v1/admin/webhooks/chat", headers=as_("ana"), json=body).status_code == 403
    assert client.put("/v1/admin/webhooks/chat", headers=as_("admin"), json={**body, "url": "ftp://x"}).status_code == 422
    assert client.put("/v1/admin/webhooks/chat", headers=as_("admin"), json={**body, "events": ["teleport"]}).status_code == 422
    capture(monkeypatch, client, events=["membership.waiting"], secret="s3cret-signing-key-0123")
    listed = client.get("/v1/admin/webhooks", headers=as_("admin")).json()
    assert listed["webhooks"][0]["has_secret"] and "secret" not in listed["webhooks"][0]
    assert "review.overdue" in {e["key"] for e in listed["events"]}
    set_email("bob", "bob@example.test")
    with SessionLocal() as db:
        bob = rbac.resolve_user(db, USERS["bob"]).principal_id
        assert notify.queue(db, "review.waiting", [bob], "Hello", "Body") == 0          # not an event this webhook wants
        assert notify.queue(db, "membership.waiting", [bob], "Hello", "Body", key="hello") == 1
        assert notify.queue(db, "membership.waiting", [bob], "Hello", "Body", key="hello") == 0   # once per key
        db.commit()
    notify.tick(SessionLocal)
    url, payload, headers, raw = Hook.raw[0]
    assert url == "https://hooks.example.test/fdp" and headers["X-FDP-Event"] == "membership.waiting"
    assert payload["recipients"] == [{"username": "bob", "email": "bob@example.test"}] and payload["subject"] == "Hello"
    assert headers["X-FDP-Signature"] == "sha256=" + hmac.new(b"s3cret-signing-key-0123", raw, hashlib.sha256).hexdigest()
    # Failures are retried, then kept with their error.

    def refuse(url, body, headers):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(notify, "_post", refuse)
    with SessionLocal() as db:
        notify.queue(db, "membership.waiting", [bob], "Again", "Body")
        db.commit()
    for _ in range(notify.MAX_ATTEMPTS + 1):
        notify.tick(SessionLocal)
    with SessionLocal() as db:
        item = db.scalar(select(Notification).where(Notification.sent_at.is_(None)))
        assert item.attempts == notify.MAX_ATTEMPTS and "refused" in item.error
    hooks = client.get("/v1/admin/webhooks", headers=as_("admin")).json()["webhooks"]
    assert hooks[0]["failing"]
    assert [d["event"] for d in client.get("/v1/admin/webhooks/privacy/deliveries", headers=as_("admin")).json()] == ["membership.waiting"] * 2
    tested = client.post("/v1/admin/webhooks/privacy/test", headers=as_("admin")).json()
    assert not tested["sent"] and "refused" in tested["error"]
    assert client.delete("/v1/admin/webhooks/privacy", headers=as_("admin")).status_code == 204
    assert client.get("/v1/admin/webhooks", headers=as_("admin")).json()["webhooks"] == []


def test_access_ends_renews_and_existing_access_gets_a_deadline(client, repo, monkeypatch):  # noqa: F811
    from datetime import timedelta
    from app import expiry, notify
    from app.models import AccessCase, JustificationDeadline, utcnow
    capture(monkeypatch, client)
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    # Access that existed before the policy: kept, with a deadline.
    legacy = grant_group(client, "workloads", "Legacy readers", "Executor")
    client.put(f"/v1/rbac/resources/{ids['public']}/assignments", headers=as_("ana"), json={"app_group_id": legacy, "role": "Reader"}).raise_for_status()
    layer, _ = set_up_policy(client, ids["public"])
    marketing = grant_group(client, "workloads", "Marketing readers", "Executor")
    privacy = grant_group(client, "workloads", "Privacy team", "Executor", users=["bob"])
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_reviewer", "app_group_id": privacy}).raise_for_status()
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_admin", "app_group_id": privacy}).raise_for_status()
    set_email("ana", "ana@example.test")
    set_email("bob", "bob@example.test")
    now = utcnow()
    expiry.tick(SessionLocal, now)
    deadlines = client.get("/v1/compliance/deadlines", headers=as_("ana"), params={"resource_id": ids["public"]}).json()
    assert [(d["group"], d["role"], d["classification"]) for d in deadlines] == [("Legacy readers", "Reader", "restricted")]
    # The group's access is approved for 25 days; a renewal opens 30 days before the end, prefilled.
    target = {"kind": "role", "resource_id": ids["public"], "role": "Reader", "app_group_id": marketing}
    case = client.post("/v1/compliance/cases", headers=as_("ana"), json={**target, "answers": ANSWERS,
                                                                         "until": (now + timedelta(days=25)).isoformat()}).json()
    approved = client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "approve"}).json()
    assert approved["status"] == "approved"
    expiry.tick(SessionLocal, now + timedelta(days=4))
    with SessionLocal() as db:
        renewal = db.scalar(select(AccessCase).where(AccessCase.renewal_of == case["id"]))
        assert renewal.status == "draft" and renewal.answers == ANSWERS and renewal.requester == "ana"
    assert client.get(f"/v1/compliance/cases/{case['id']}", headers=as_("ana")).json()["renewal"] == renewal.id
    expiry.tick(SessionLocal, now + timedelta(days=12))   # 13 days left, nothing sent: a reminder
    expiry.tick(SessionLocal, now + timedelta(days=12))   # once
    notify.tick(SessionLocal)
    subjects = [(m["to"], m["subject"]) for m in Hook.sent]
    assert ("ana@example.test", "Renew access: Reader on public for Marketing readers") in subjects
    assert sum(s.startswith("Access ends in 13 days") for _, s in subjects) == 1, subjects
    assert any(s.startswith("Justify access by") for _, s in subjects)
    # Without a renewal the access ends; the role goes away and the case is expired.
    expiry.tick(SessionLocal, now + timedelta(days=31))
    with SessionLocal() as db:
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == ids["public"], RoleAssignment.app_group_id == marketing)) is None
        assert db.get(AccessCase, case["id"]).status == "expired"
        assert db.scalar(select(JustifiedGrant).where(JustifiedGrant.app_group_id == marketing)) is None
    # Past the deadline, the legacy access without a form is removed too.
    with SessionLocal() as db:
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == ids["public"], RoleAssignment.app_group_id == legacy)) is None
        assert db.scalar(select(JustificationDeadline)) is None
    # An ended case can be renewed by hand.
    again = client.post(f"/v1/compliance/cases/{case['id']}/renew", headers=as_("ana"))
    assert again.status_code == 201 and again.json()["renewal_of"] == case["id"]


def test_justifying_existing_access_clears_its_deadline_and_overdue_reviews_escalate(client, repo, monkeypatch):  # noqa: F811
    from datetime import timedelta
    from app import expiry, notify
    from app.models import JustificationDeadline, utcnow
    capture(monkeypatch, client)
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    legacy = grant_group(client, "workloads", "Legacy readers", "Executor")
    client.put(f"/v1/rbac/resources/{ids['public']}/assignments", headers=as_("ana"), json={"app_group_id": legacy, "role": "Reader"}).raise_for_status()
    set_up_policy(client, ids["public"])
    privacy = grant_group(client, "workloads", "Privacy team", "Executor", users=["bob"])
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_admin", "app_group_id": privacy}).raise_for_status()
    set_email("bob", "bob@example.test")
    now = utcnow()
    expiry.tick(SessionLocal, now)
    # The owners justify the access the group already has.
    case = client.post("/v1/compliance/cases", headers=as_("ana"), json={"kind": "role", "resource_id": ids["public"], "role": "Reader",
                                                                         "app_group_id": legacy, "answers": ANSWERS}).json()
    expiry.tick(SessionLocal, now + timedelta(days=11))   # review due after 10 days: overdue
    notify.tick(SessionLocal)
    assert ("bob@example.test", "Review overdue: Reader on public for Legacy readers") in [(m["to"], m["subject"]) for m in Hook.sent]
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_reviewer", "app_group_id": privacy}).raise_for_status()
    client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_("bob"), json={"decision": "approve"}).raise_for_status()
    with SessionLocal() as db:
        assert db.scalar(select(JustificationDeadline)) is None
        assert db.scalar(select(JustifiedGrant).where(JustifiedGrant.app_group_id == legacy)).previous_role is None
    expiry.tick(SessionLocal, now + timedelta(days=20))   # justified now: no new deadline, the access stays
    with SessionLocal() as db:
        assert db.scalar(select(JustificationDeadline)) is None
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == ids["public"], RoleAssignment.app_group_id == legacy)).role == "Reader"


def test_the_datas_owners_need_no_form_below_what_they_own(client, repo):  # noqa: F811
    from app import expiry
    from app.models import JustificationDeadline, utcnow
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}})
    with SessionLocal() as db:
        source = db.get(Resource, ids["public"]).parent_id
    owners = grant_group(client, "workloads", "Lake owners", "Executor")
    client.put(f"/v1/rbac/resources/{source}/assignments", headers=as_("admin"), json={"app_group_id": owners, "role": "Owner"}).raise_for_status()
    set_up_policy(client, ids["public"])
    # A role below what they own needs no form, and gets no deadline.
    assert client.put(f"/v1/rbac/resources/{ids['public']}/assignments", headers=as_("admin"),
                      json={"app_group_id": owners, "role": "Admin"}).status_code == 200
    expiry.tick(SessionLocal, utcnow())
    with SessionLocal() as db:
        assert db.scalar(select(JustificationDeadline).where(JustificationDeadline.app_group_id == owners)) is None
    # Becoming an owner of covered data does need one.
    others = grant_group(client, "workloads", "Others", "Executor")
    refused = client.put(f"/v1/rbac/resources/{source}/assignments", headers=as_("admin"), json={"app_group_id": others, "role": "Owner"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "justification_required"

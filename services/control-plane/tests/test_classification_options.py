"""Configurable classifications, column classification, and data product access options (masks per option)."""
from sqlalchemy import select

from app import catalog, product_options, rbac
from app.db import SessionLocal
from app.models import AppGroup, DataSource, ProductAccess
from .conftest import USERS, as_
from .test_deploying import repo  # noqa: F401
from .test_products import setup_products


def classify(client, asset_id, columns, user="ana"):
    return client.put(f"/v1/catalog/assets/{asset_id}/columns", headers=as_(user), json={"columns": columns})


def test_classifications_are_configurable_and_objects_never_below_their_columns(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    assert [l["name"] for l in client.get("/v1/catalog/classifications", headers=as_("ana")).json()["levels"]] == \
        ["public", "internal", "confidential", "restricted"]
    # Columns carry the level; the table follows the highest.
    assert classify(client, ids["sales"], {"amount": {"classification": "internal"}}, user="bob").status_code in (403, 404)
    out = classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["PII"]}, "amount": {"classification": "internal"}})
    assert out.status_code == 200, out.text
    level = out.json()["classification"]
    assert level["inherited"] == "restricted" and level["effective"] == "restricted" and level["columns"]["email"]["tags"] == ["pii"]
    # A table can be classified higher than its columns, never lower.
    governance = {"description": "", "domain": "Finance", "tags": []}
    lower = client.put(f"/v1/catalog/assets/{ids['sales']}/governance", headers=as_("ana"), json={**governance, "classification": "internal"})
    assert lower.status_code == 422 and "at least restricted" in lower.text
    classify(client, ids["sales"], {"email": {"classification": "internal"}})
    higher = client.put(f"/v1/catalog/assets/{ids['sales']}/governance", headers=as_("ana"), json={**governance, "classification": "confidential"})
    assert higher.status_code == 200 and higher.json()["classification"]["effective"] == "confidential"
    # Governance edits keep the column classifications.
    assert higher.json()["classification"]["columns"]["email"]["classification"] == "internal"
    # The list is configuration: add a level, keep what is in use.
    new = [{"name": "public"}, {"name": "internal"}, {"name": "confidential", "masked_as": "internal"},
           {"name": "restricted", "masked_as": "internal"}, {"name": "secret", "masked_as": "confidential"}]
    assert client.put("/v1/catalog/classifications", headers=as_("ana"), json={"levels": new}).status_code == 403
    saved = client.put("/v1/catalog/classifications", headers=as_("admin"), json={"levels": new})
    assert saved.status_code == 200 and saved.json()["levels"][-1] == {"name": "secret", "masked_as": "confidential", "description": ""}
    bad = client.put("/v1/catalog/classifications", headers=as_("admin"), json={"levels": [{"name": "public", "masked_as": "internal"}, {"name": "internal"}]})
    assert bad.status_code == 422
    in_use = client.put("/v1/catalog/classifications", headers=as_("admin"), json={"levels": [{"name": "public"}, {"name": "internal"}]})
    assert in_use.status_code == 409 and "confidential" in in_use.text


def scope_for(user):
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name == "finance-dw"))
        caller = rbac.resolve_user(db, USERS[user])
        return product_options.attach(db, caller, {"finance_dw": {"source_id": source.id, "kind": "postgres", "schemas": {"public": "Reader"}}})


def masked_sales(client,repo):
    ids=setup_products(client,repo)
    response=client.post('/v1/catalog/products',headers=as_('ana'),json={
        'title':'Masked sales','asset_ids':[ids['sales']], 'options':[{'name':'Masked','default':True,
        'masks':[{'table':'sales','column':'email','method':'hash'}]}]})
    assert response.status_code==201,response.text
    product=response.json()
    with SessionLocal() as db:
        group=db.scalar(select(AppGroup).where(AppGroup.name=='Deployed executors')).id
    client.put('/v1/catalog/products/'+product['id']+'/consumers/'+group,
        headers=as_('ana'),json={}).raise_for_status()
    return ids,product,group


def test_metadata_role_cannot_remove_product_option_masks(client,repo):
    ids,_,group=masked_sales(client,repo)
    for role in ('Reader','Contributor'):
        client.put('/v1/rbac/resources/'+ids['public']+'/assignments',headers=as_('admin'),
            json={'app_group_id':group,'role':role}).raise_for_status()
        assert scope_for('bob')['finance_dw']['policies']['public.sales']['masks']['email']['method']=='hash'


def test_admitted_job_keeps_product_masks_after_live_access_is_revoked(client,repo,monkeypatch):
    from app import sql_access
    from app.auth import Execution
    from app.models import Resource,Run
    from app.environments import execution_user
    from .test_api import start_workspace
    ids,product,group=masked_sales(client,repo)
    with SessionLocal() as db:
        schema=db.get(Resource,ids['public']);schema.config={**schema.config,'state':'ready'};db.commit()
    workspace=start_workspace(client,user='bob')
    response=client.post('/v1/runs',headers=as_('bob'),json={
        'workspace_id':workspace,'pool':'small','kind':'script','entrypoint':'analysis.py'})
    assert response.status_code==201,response.text
    ident=response.json()['id']
    client.delete('/v1/catalog/products/'+product['id']+'/consumers/'+group,headers=as_('ana')).raise_for_status()
    monkeypatch.setattr(sql_access,'check_native_relations',lambda query:None)
    with SessionLocal() as db:
        run=db.get(Run,ident)
        actor=rbac.resolve_user(db,execution_user(db,{'sub':'bob','pid':run.principal_id,'run':ident}))
        query=sql_access.admit(db,sql_access.SqlActor(actor,'test',execution=Execution(
            username='bob',groups=[],workspace_id=workspace,run_id=ident,principal_id=actor.principal_id)), 'finance_dw')
        assert query.scope['finance_dw']['policies']['public.sales']['masks']['email']['method']=='hash'


def test_cached_metrics_cannot_bypass_product_masks_or_metadata_only_access(client,repo):
    ids,_,group=masked_sales(client,repo)
    response=client.post('/v1/catalog/assets/'+ids['sales']+'/metrics',headers=as_('admin'),json={
        'key':'private-total','name':'Private total','kind':'kpi'})
    assert response.status_code==201,response.text
    metric=response.json()
    base='/v1/catalog/assets/'+ids['sales']+'/metrics'
    client.post(base+'/'+metric['id']+'/observations',headers=as_('admin'),
        json={'value':987654.321,'idempotency_key':'sensitive-total'}).raise_for_status()
    client.put('/v1/rbac/resources/'+ids['public']+'/assignments',headers=as_('admin'),
        json={'app_group_id':group,'role':'Reader'}).raise_for_status()
    response=client.get(base,headers=as_('bob'))
    assert response.status_code==200,response.text
    assert response.json()[0]['latest'] is None
    assert response.json()[0]['observations_restricted']
    assert '987654.321' not in response.text
    assert client.get(base+'/'+metric['id']+'/history',headers=as_('bob')).status_code==403
    # An explicit, unfiltered schema read grants these values as well as SQL rows.
    client.put('/v1/rbac/resources/'+ids['public']+'/assignments',headers=as_('admin'),
        json={'app_group_id':group,'role':'DataReader'}).raise_for_status()
    response=client.get(base,headers=as_('bob'))
    assert response.json()[0]['latest']['value']==987654.321


def test_options_mask_what_consumers_see_and_set_the_level(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    classify(client, ids["sales"], {"email": {"classification": "restricted", "tags": ["pii"]}, "amount": {"classification": "internal"}})
    with SessionLocal() as db:
        public = ids["public"]
    # Published as Terraform would: objects by resource, options with masks and a row filter.
    product = client.post("/v1/catalog/products", headers=as_("ana"), json={
        "title": "Sales", "members": [{"resource_id": public, "object_path": "sales"}],
        "options": [
            {"name": "Masked", "description": "Email hashed", "default": True,
             "masks": [{"resource_id": public, "table": "sales", "column": "email", "method": "hash"}]},
            {"name": "Own region", "masks": [{"table": "sales", "column": "email", "method": "redact"}],
             "filters": [{"table": "sales", "expression": "region = 'EU'"}]},
            {"name": "Full", "description": "Everything"}]})
    assert product.status_code == 201, product.text
    p = product.json()
    levels = {o["name"]: o["classification"] for o in p["options"]}
    assert levels == {"Masked": "internal", "Own region": "internal", "Full": "restricted"} and p["classification"] == "restricted"
    assert p["options"][0]["masks"][0]["table"] == "sales"  # owners see the masks
    # Consumers and requesters see the options, not which columns.
    bob_view = client.get("/v1/catalog/products", headers=as_("bob")).json()["items"][0]
    assert "masks" not in bob_view["options"][0] and bob_view["options"][0]["masked_columns"] == 1
    # A request asks for an option; approval gives that option.
    with SessionLocal() as db:
        executors = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors")).id
    full = next(o for o in p["options"] if o["name"] == "Full")["id"]
    masked = next(o for o in p["options"] if o["name"] == "Masked")["id"]
    request = client.post(f"/v1/catalog/products/{p['id']}/requests", headers=as_("bob"),
                          json={"app_group_id": executors, "reason": "Monthly report", "option_id": masked})
    assert request.status_code == 201 and request.json()["option"] == "Masked"
    client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": True}).raise_for_status()
    # Bob reaches sales only through the product: his queries get the Masked option's mask.
    policies = scope_for("bob")["finance_dw"]["policies"]["public.sales"]
    assert policies["masks"] == {"email": {"method": "hash", "expression": ""}} and policies["filter"] is None
    # Owners move the group to another option.
    region = next(o for o in p["options"] if o["name"] == "Own region")["id"]
    assert client.put(f"/v1/catalog/products/{p['id']}/consumers/{executors}", headers=as_("ana"), json={"option_id": region}).status_code == 200
    policies = scope_for("bob")["finance_dw"]["policies"]["public.sales"]
    assert policies["masks"]["email"]["method"] == "redact" and policies["filter"] == "(region = 'EU')"
    client.put(f"/v1/catalog/products/{p['id']}/consumers/{executors}", headers=as_("ana"), json={"option_id": full}).raise_for_status()
    assert "policies" not in scope_for("bob")["finance_dw"]
    detail = client.get(f"/v1/catalog/products/{p['id']}", headers=as_("ana")).json()
    assert [(x["group"], x["option"]) for x in detail["consumers"]] == [("Deployed executors", "Full")]
    # Direct access to the schema is not limited by a product option.
    client.put(f"/v1/catalog/products/{p['id']}/consumers/{executors}", headers=as_("ana"), json={"option_id": masked}).raise_for_status()
    assert "policies" in scope_for("bob")["finance_dw"]
    client.put(f"/v1/rbac/resources/{public}/assignments", headers=as_("admin"), json={"app_group_id": executors, "role": "DataReader"}).raise_for_status()
    assert "policies" not in scope_for("bob")["finance_dw"]
    # Removing an option moves its consumers to the default.
    client.patch(f"/v1/catalog/products/{p['id']}", headers=as_("ana"), json={"options": [
        {"name": "Masked", "default": True, "masks": [{"table": "sales", "column": "email", "method": "hash"}]}]}).raise_for_status()
    with SessionLocal() as db:
        assert db.scalar(select(ProductAccess).where(ProductAccess.app_group_id == executors)).option_id == masked
    assert client.delete(f"/v1/catalog/products/{p['id']}/consumers/{executors}", headers=as_("ana")).status_code == 204
    assert client.get(f"/v1/catalog/products/{p['id']}", headers=as_("ana")).json()["consumers"] == []


def test_option_masks_must_name_product_tables(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    bad = client.post("/v1/catalog/products", headers=as_("ana"), json={"title": "x", "asset_ids": [ids["sales"]],
                      "options": [{"name": "Masked", "masks": [{"table": "raw_sales", "column": "email"}]}]})
    assert bad.status_code == 422 and "table in the product" in bad.text
    twice = client.post("/v1/catalog/products", headers=as_("ana"), json={"title": "x", "asset_ids": [ids["sales"]],
                        "options": [{"name": "A", "default": True}, {"name": "B", "default": True}]})
    assert twice.status_code == 422
    standard = client.post("/v1/catalog/products", headers=as_("ana"), json={"title": "x", "asset_ids": [ids["sales"]]}).json()
    assert [o["name"] for o in standard["options"]] == ["Standard"] and standard["options"][0]["default"]


def test_no_classifications_until_configured_and_values_in_use_must_stay(client, repo):  # noqa: F811
    from app import classification
    from app.models import CatalogAsset, PlatformSetting
    ids = setup_products(client, repo)
    with SessionLocal() as db:
        db.delete(db.get(PlatformSetting, classification.SETTING))
        # A value from before the levels were configurable.
        asset = db.get(CatalogAsset, ids["sales"])
        asset.governance = {**(asset.governance or {}), "classification": "confidential"}
        db.commit()
    assert client.get("/v1/catalog/classifications", headers=as_("ana")).json()["levels"] == []
    assert classify(client, ids["sales"], {"email": {"classification": "restricted"}}).status_code == 422
    missing = client.put("/v1/catalog/classifications", headers=as_("admin"), json={"levels": [{"name": "public"}, {"name": "internal"}]})
    assert missing.status_code == 409 and "confidential is still used" in missing.text
    assert client.put("/v1/catalog/classifications", headers=as_("admin"),
                      json={"levels": [{"name": "public"}, {"name": "confidential"}]}).status_code == 200

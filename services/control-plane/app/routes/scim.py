"""Provider-neutral SCIM 2.0 Users/Groups with stable IDs and soft deletion."""
import hmac
import re
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from ..config import get_settings
from ..db import get_db
from ..models import DirectoryPrincipal as Principal, DirectoryMembership as Membership
from .. import rbac
from .common import audit

CORE = "urn:ietf:params:scim:schemas:core:2.0:"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"


def authenticated(authorization: Optional[str] = Header(default=None)):
    configured = get_settings().scim_token
    if not configured or not authorization or not hmac.compare_digest(authorization, f"Bearer {configured}"):
        raise HTTPException(401, "invalid SCIM provisioning credential")


class SCIMResponse(JSONResponse):
    media_type = "application/scim+json"

router = APIRouter(prefix="/scim/v2", tags=["SCIM"], dependencies=[Depends(authenticated)],
                   default_response_class=SCIMResponse)

async def read_body(request):
    import json
    body = await request.body()
    if len(body) > 1024 * 1024:
        fail(413, "provisioning request is too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        fail(400, "invalid JSON", "invalidSyntax")
    if not isinstance(data, dict):
        fail(400, "resource must be an object")
    return data


def fail(code, detail, typ="invalidValue"):
    raise HTTPException(code, {"schemas": [ERROR], "status": str(code), "scimType": typ, "detail": detail})


def rows(db, kind):
    return list(db.scalars(select(Principal).where(Principal.kind == kind,
                        Principal.provider == get_settings().oidc_issuer, Principal.managed.is_(True))))


def find(db, kind, pid):
    p = next((x for x in rows(db, kind) if x.id == pid and not x.attributes.get("deleted")), None)
    if not p:
        fail(404, "resource not found")
    return p


def output(db, p):
    typ = "User" if p.kind == "user" else "Group"
    result = {"schemas": [CORE + typ], "id": p.id, "externalId": p.external_id,
              "meta": {"resourceType": typ, "location": f"/scim/v2/{typ}s/{p.id}"}}
    if p.kind == "user":
        result.update(userName=p.name, active=p.active, **{k: v for k, v in p.attributes.items()
                      if k in ("name", "displayName", "emails", "roles")})
    else:
        result["displayName"] = p.name
        result["members"] = [{"value": m.member_id,
                               "type": "Group" if db.get(Principal, m.member_id).kind == "group" else "User"}
                              for m in db.scalars(select(Membership).where(Membership.group_id == p.id))]
    return result


def validate_members(db, p, members):
    ids = []
    if not isinstance(members, list):
        fail(400, "members must be a list")
    for member in members:
        mid = member.get("value") if isinstance(member, dict) else None
        target = db.get(Principal, mid) if isinstance(mid, str) else None
        if not target or target.kind not in ("group", "user") or not target.active or target.provider != p.provider:
            fail(400, "unknown or inactive group member")
        if mid == p.id:
            fail(400, "group membership cycle")
        # If the target is already an ancestor of this group, adding it closes a cycle.
        if target.kind == "group" and mid in rbac.closure(db, p.id):
            fail(400, "group membership cycle")
        ids.append(mid)
    return set(ids)


def save(db, p, data):
    p = db.scalar(select(Principal).where(Principal.id == p.id).with_for_update().execution_options(populate_existing=True))
    name_key = "userName" if p.kind == "user" else "displayName"
    name = data.get(name_key, p.name)
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        fail(400, f"{name_key} is required")
    if any(x.id != p.id and x.name.casefold() == name.casefold() and not x.attributes.get("deleted")
           for x in rows(db, p.kind)):
        fail(409, f"{name_key} already exists", "uniqueness")
    external = data.get("externalId", p.external_id)
    if not isinstance(external, str) or len(external) > 256:
        fail(400, "externalId must be a string of at most 256 characters")
    if any(x.id != p.id and x.external_id == external for x in rows(db, p.kind)):
        fail(409, "externalId already exists", "uniqueness")
    if "active" in data and not isinstance(data["active"], bool):
        fail(400, "active must be a boolean")
    attrs = {**p.attributes, **{k: v for k, v in data.items() if k in ("name", "displayName", "emails", "roles")}}
    if p.kind == "user":
        role_values = data.get("roles", attrs.get("roles", []))
        if not isinstance(role_values, list) or any(not isinstance(x, dict) or not isinstance(x.get("value"), str)
                                                  for x in role_values):
            fail(400, "roles must contain string values")
    members = validate_members(db, p, data["members"]) if p.kind == "group" and "members" in data else None
    p.name, p.external_id, p.active, p.attributes, p.managed = name, external, data.get("active", p.active), attrs, True
    if members is not None:
        db.execute(delete(Membership).where(Membership.group_id == p.id))
        for mid in members:
            db.add(Membership(group_id=p.id, member_id=mid))
    if p.kind == "user" and "roles" in data:
        for m in list(db.scalars(select(Membership).where(Membership.member_id == p.id))):
            if db.get(Principal, m.group_id).kind == "role":
                db.delete(m)
        db.flush()
        for role in {x["value"] for x in data["roles"]}:
            g = rbac.principal(db, "role", role, role, p.provider)
            g.managed = True
            db.add(Membership(group_id=g.id, member_id=p.id))
    db.flush()


@router.get("/ServiceProviderConfig")
def provider_config():
    return {"schemas": [CORE + "ServiceProviderConfig"], "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 1000}, "sort": {"supported": False},
            "etag": {"supported": False}, "changePassword": {"supported": False},
            "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Provisioning bearer token",
                                       "description": "Dedicated SCIM bearer credential"}]}


@router.get("/ResourceTypes")
def resource_types():
    entries = [{"schemas": [CORE + "ResourceType"], "id": t, "name": t, "endpoint": f"/{t}s", "schema": CORE+t}
               for t in ("User", "Group")]
    return {"schemas": [LIST], "totalResults": 2, "Resources": entries}


@router.get("/Schemas")
def schemas():
    return {"schemas": [LIST], "totalResults": 2, "Resources": [
        {"schemas": [CORE+"Schema"], "id": CORE+t, "name": t, "attributes": attrs} for t, attrs in (
            ("User", [{"name": "userName", "type": "string", "required": True, "multiValued": False},
                      {"name": "active", "type": "boolean", "multiValued": False}]),
            ("Group", [{"name": "displayName", "type": "string", "required": True, "multiValued": False},
                       {"name": "members", "type": "complex", "multiValued": True,
                        "subAttributes": [{"name": "value", "type": "string", "multiValued": False}]}]))]}


@router.get("/Schemas/{schema_id}")
def schema(schema_id: str):
    entry = next((s for s in schemas()["Resources"] if s["id"] == schema_id), None)
    if entry is None:
        fail(404, "schema not found")
    return entry


@router.get("/ResourceTypes/{type_id}")
def resource_type(type_id: str):
    entry = next((s for s in resource_types()["Resources"] if s["id"] == type_id), None)
    if entry is None:
        fail(404, "resource type not found")
    return entry


@router.get("/{collection}")
def list_resources(collection: str, filter: Optional[str] = None, startIndex: int = 1, count: int = 100,
                   db: Session = Depends(get_db)):
    kind = {"Users": "user", "Groups": "group"}.get(collection)
    if not kind:
        fail(404, "unknown resource type")
    result = [output(db, p) for p in rows(db, kind) if not p.attributes.get("deleted")]
    if filter:
        match = re.fullmatch(r'(userName|displayName|externalId|id)\s+eq\s+"([^"\\]*)"', filter, re.I)
        if not match:
            fail(400, "supported filter: attribute eq \"value\"", "invalidFilter")
        key = {"username": "userName", "displayname": "displayName", "externalid": "externalId", "id": "id"}[match[1].lower()]
        result = [x for x in result if str(x.get(key, "")).casefold() == match[2].casefold()]
    startIndex, count = max(1, startIndex), max(0, min(count, 1000))
    page = result[startIndex-1:startIndex-1+count]
    return {"schemas": [LIST], "totalResults": len(result), "startIndex": startIndex,
            "itemsPerPage": len(page), "Resources": page}


@router.get("/{collection}/{pid}")
def get_resource(collection: str, pid: str, db: Session = Depends(get_db)):
    if collection not in ("Users", "Groups"):
        fail(404, "unknown resource type")
    return output(db, find(db, "user" if collection == "Users" else "group", pid))


@router.post("/{collection}", status_code=201)
async def create_resource(collection: str, request: Request, db: Session = Depends(get_db)):
    if collection not in ("Users", "Groups"):
        fail(404, "unknown resource type")
    data = await read_body(request)
    if not isinstance(data, dict):
        fail(400, "resource must be an object")
    kind = "user" if collection == "Users" else "group"
    name = data.get("userName" if kind == "user" else "displayName", "")
    # Adopt an OIDC-only principal to preserve app bindings when SCIM is enabled later.
    p = next((x for x in db.scalars(select(Principal).where(Principal.kind == kind,
             Principal.provider == get_settings().oidc_issuer, Principal.name == name)) if not x.managed), None)
    restored = False
    if p is None and isinstance(data.get("externalId"), str):
        p = next((x for x in rows(db, kind) if x.external_id == data["externalId"] and x.attributes.get("deleted")), None)
        if p:
            p.attributes = {k: v for k, v in p.attributes.items() if k != "deleted"}
            p.active, restored = True, True
    if p is None:
        from ..models import new_id
        p = Principal(kind=kind, provider=get_settings().oidc_issuer, name="", external_id=new_id(), attributes={}, active=True)
        db.add(p)
        db.flush()
    if p.kind == "user" and not p.managed:
        db.execute(delete(Membership).where(Membership.member_id == p.id))
    save(db, p, data)
    audit(db, "scim", "directory.restore" if restored else "directory.create", p.id, kind=kind)
    db.commit()
    return JSONResponse(output(db, p), status_code=201, media_type="application/scim+json",
                        headers={"Location": f"/scim/v2/{collection}/{p.id}"})


@router.put("/{collection}/{pid}")
@router.patch("/{collection}/{pid}")
async def update_resource(collection: str, pid: str, request: Request, db: Session = Depends(get_db)):
    if collection not in ("Users", "Groups"):
        fail(404, "unknown resource type")
    p = find(db, "user" if collection == "Users" else "group", pid)
    data = await read_body(request)
    if not isinstance(data, dict):
        fail(400, "resource must be an object")
    if request.method == "PATCH":
        operations = data.get("Operations")
        if not isinstance(operations, list) or not operations:
            fail(400, "Operations must be a nonempty list")
        for op in operations:
            if not isinstance(op, dict):
                fail(400, "PATCH operation must be an object")
            verb = str(op.get("op", "")).lower()
            path, value = op.get("path", ""), op.get("value")
            if not isinstance(path, str):
                fail(400, "PATCH path must be a string", "invalidPath")
            if verb not in ("add", "replace", "remove"):
                fail(400, "unsupported PATCH operation")
            if not path:
                if verb == "remove" or not isinstance(value, dict):
                    fail(400, "PATCH requires a path or object value")
                save(db, p, value)
            elif p.kind == "group" and (path == "members" or path.startswith("members[")):
                members = output(db, p)["members"]
                if path != "members":
                    match = re.fullmatch(r'members\[value\s+eq\s+"([^"\\]+)"\]', path)
                    if not match or verb != "remove":
                        fail(400, "unsupported member PATCH path", "invalidPath")
                    members = [m for m in members if m["value"] != match[1]]
                elif verb == "replace":
                    members = value
                elif verb == "add":
                    if not isinstance(value, list):
                        fail(400, "members must be a list")
                    members += value
                elif value is None:
                    members = []
                else:
                    if not isinstance(value, list):
                        fail(400, "members must be a list")
                    ids = {m.get("value") for m in value}
                    members = [m for m in members if m["value"] not in ids]
                save(db, p, {"members": members})
            elif path in ("userName", "displayName", "active", "externalId", "roles", "name", "emails"):
                if verb == "remove" and path in ("userName", "displayName", "active", "externalId"):
                    fail(400, "required identity attributes cannot be removed", "mutability")
                save(db, p, {path: ([] if path in ("roles", "emails") else {}) if verb == "remove" else value})
            else:
                fail(400, "unsupported PATCH path", "invalidPath")
    else:
        # PUT replaces optional attributes/membership; principal ID and OIDC subject survive.
        defaults = {"roles": [], "emails": [], "name": {}, "active": True} if p.kind == "user" else {"members": []}
        save(db, p, {**defaults, **data})
    audit(db, "scim", "directory.update", p.id, active=p.active)
    db.commit()
    return output(db, p)


@router.delete("/{collection}/{pid}", status_code=204)
def delete_resource(collection: str, pid: str, db: Session = Depends(get_db)):
    if collection not in ("Users", "Groups"):
        fail(404, "unknown resource type")
    p = find(db, "user" if collection == "Users" else "group", pid)
    p.active, p.attributes = False, {**p.attributes, "deleted": True}
    db.execute(delete(Membership).where((Membership.member_id == p.id) | (Membership.group_id == p.id)))
    audit(db, "scim", "directory.delete", p.id)
    db.commit()

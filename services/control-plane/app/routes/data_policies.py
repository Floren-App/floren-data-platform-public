"""Column masks and row filters on governed tables: list, create, change and remove them.

Deciding who sees which values is an assignment decision, so changes require `assign` on the
schema (its Owners and global administrators). Anyone who may read the schema may see its
policies, so people know when what they see is masked or filtered.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import data_policies, rbac
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import AppGroup, DataPolicy, DataSource, DirectoryPrincipal, Resource
from .common import audit

router = APIRouter(prefix="/v1/data-policies", tags=["data policies"])


class PolicyChange(BaseModel):
    method: str = Field(default="", description="Column masks: null, redact, hash, partial or expression")
    expression: str = Field(default="", max_length=data_policies.MAX_EXPRESSION,
        description="The mask expression (method expression) or the row filter condition, in Trino SQL")
    exempt_groups: List[str] = Field(default_factory=list, description="App group IDs that see the real values / all rows")
    exempt_principals: List[str] = Field(default_factory=list, description="Identity IDs (people or managed identities) exempted")
    description: str = Field(default="", max_length=1024)


class PolicyIn(PolicyChange):
    schema_id: str = Field(description="The governed schema (its resource ID)")
    table: str = Field(min_length=1, max_length=256)
    kind: str = Field(description="column_mask or row_filter")
    column: str = Field(default="", max_length=256, description="Column masks: the masked column")


class PolicyOut(BaseModel):
    id: str
    schema_id: str
    table: str
    kind: str
    column: str
    method: str
    expression: str
    exempt_groups: List[str]
    exempt_principals: List[str]
    member_groups: List[str] = Field(description="Groups named by is_member() in the expression")
    description: str
    updated_by: str
    updated_at: Optional[datetime]


def out(policy: DataPolicy) -> PolicyOut:
    return PolicyOut(id=policy.id, schema_id=policy.schema_id, table=policy.table_name, kind=policy.kind,
        column=policy.column_name, method=policy.method, expression=policy.expression,
        exempt_groups=policy.exempt_groups or [], exempt_principals=policy.exempt_principals or [],
        member_groups=data_policies.member_groups(policy.expression) if policy.expression else [],
        description=policy.description, updated_by=policy.updated_by, updated_at=policy.updated_at)


def governed_schema(db: Session, schema_id: str) -> Resource:
    from ..sql_access import SQL_KINDS
    schema = db.get(Resource, schema_id)
    source = db.get(DataSource, schema.object_id) if schema and schema.kind == "schema" else None
    if not source:
        raise HTTPException(404, "schema not found")
    if source.kind not in SQL_KINDS:
        raise HTTPException(409, "column masks and row filters apply to PostgreSQL and Iceberg tables")
    return schema


def check(db: Session, body: PolicyChange, kind: str, column: str):
    method, expression = data_policies.validate(kind, body.method, body.expression, column)
    groups = sorted(set(body.exempt_groups))
    if groups and len(list(db.scalars(select(AppGroup.id).where(AppGroup.id.in_(groups))))) != len(groups):
        raise HTTPException(422, "an exempt group does not exist")
    principals = sorted(set(body.exempt_principals))
    if principals and len(list(db.scalars(select(DirectoryPrincipal.id).where(DirectoryPrincipal.id.in_(principals))))) != len(principals):
        raise HTTPException(422, "an exempt identity does not exist")
    named = data_policies.member_groups(expression) if expression else []
    known = set(db.scalars(select(AppGroup.name).where(AppGroup.name.in_(named)))) if named else set()
    missing = [n for n in named if n not in known]
    if missing:
        raise HTTPException(422, "is_member names an unknown group: " + ", ".join(missing))
    return method, expression, groups, principals


def detail(policy: DataPolicy) -> dict:
    return {"schema_id": policy.schema_id, "table": policy.table_name, "kind": policy.kind, "column": policy.column_name,
            "method": policy.method, "expression": policy.expression, "exempt_groups": policy.exempt_groups,
            "exempt_principals": policy.exempt_principals}


@router.get("", response_model=List[PolicyOut])
def list_policies(schema_id: Optional[str] = Query(default=None), db: Session = Depends(get_db),
                  user: User = Depends(get_actor)):
    query = select(DataPolicy).order_by(DataPolicy.schema_id, DataPolicy.table_name, DataPolicy.kind, DataPolicy.column_name)
    if schema_id:
        query = query.where(DataPolicy.schema_id == schema_id)
    visible = {}
    result = []
    for policy in db.scalars(query):
        if policy.schema_id not in visible:
            visible[policy.schema_id] = rbac.allowed(db, user, "read", db.get(Resource, policy.schema_id))
        if visible[policy.schema_id]:
            result.append(out(policy))
    return result


class ExpressionIn(BaseModel):
    expression: str = Field(max_length=data_policies.MAX_EXPRESSION)


@router.post("/normalize")
def normalize(body: ExpressionIn, user: User = Depends(get_actor)):
    """How the platform would store an expression (so tools can compare without false changes)."""
    expression = data_policies.normalize_expression(body.expression)
    return {"expression": expression, "member_groups": data_policies.member_groups(expression)}


@router.get("/{policy_id}", response_model=PolicyOut)
def get_policy(policy_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    policy = db.get(DataPolicy, policy_id)
    if not policy or not rbac.allowed(db, user, "read", db.get(Resource, policy.schema_id)):
        raise HTTPException(404, "policy not found")
    return out(policy)


@router.post("", response_model=PolicyOut, status_code=201)
def create_policy(body: PolicyIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    schema = governed_schema(db, body.schema_id)
    rbac.require(db, user, "assign", schema)
    table = data_policies.normalize_name(body.table, "table")
    column = data_policies.normalize_name(body.column, "column") if body.column else ""
    method, expression, groups, principals = check(db, body, body.kind, column)
    policy = DataPolicy(schema_id=schema.id, table_name=table, kind=body.kind, column_name=column, method=method,
                        expression=expression, exempt_groups=groups, exempt_principals=principals,
                        description=body.description, updated_by=user.username)
    db.add(policy)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        what = "a row filter" if body.kind == "row_filter" else "a mask on " + column
        raise HTTPException(409, f"{table} already has {what}; change that policy instead") from None
    audit(db, user.username, "data_policy.create", policy.id, **detail(policy))
    db.commit()
    return out(policy)


@router.put("/{policy_id}", response_model=PolicyOut)
def update_policy(policy_id: str, body: PolicyChange, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Change how a policy masks or filters and who is exempt. Table, kind and column are fixed."""
    policy = db.get(DataPolicy, policy_id)
    schema = db.get(Resource, policy.schema_id) if policy else None
    if not policy or not rbac.allowed(db, user, "read", schema):
        raise HTTPException(404, "policy not found")
    rbac.require(db, user, "assign", schema)
    method, expression, groups, principals = check(db, body, policy.kind, policy.column_name)
    policy.method, policy.expression, policy.exempt_groups, policy.exempt_principals = method, expression, groups, principals
    policy.description, policy.updated_by = body.description, user.username
    audit(db, user.username, "data_policy.update", policy.id, **detail(policy))
    db.commit()
    return out(policy)


@router.delete("/{policy_id}", status_code=204)
def delete_policy(policy_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    policy = db.get(DataPolicy, policy_id)
    schema = db.get(Resource, policy.schema_id) if policy else None
    if not policy or not rbac.allowed(db, user, "read", schema):
        raise HTTPException(404, "policy not found")
    rbac.require(db, user, "assign", schema)
    audit(db, user.username, "data_policy.delete", policy.id, **detail(policy))
    db.delete(policy)
    db.commit()

"""Validated management SQL on the metadata catalog, shared by both SQL protocols.

Reads run in Trino. Writes dispatch to the same commands as the UI; the engine's
database account cannot mutate operational tables, even if a wrapper is missed.
"""
import json
import logging
import re
from datetime import timedelta
from urllib.parse import unquote_plus

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlglot import exp, parse_one, errors as sql_errors
from sqlglot.tokens import TokenType

from . import platform_metadata as metadata, sql_access
from .config import get_settings
from .models import AppGroup, CatalogAsset, IdentityBinding, Resource, RoleAssignment, ServiceRoleAssignment, SqlQuery, utcnow


def fail(message):
    raise HTTPException(422, message)


def scalar(node):
    if isinstance(node, exp.Literal):
        if not node.is_string:
            fail("Metadata changes require quoted string values")
        return node.this
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Array):
        return [scalar(v) for v in node.expressions]
    fail("Metadata changes accept literal values only; subqueries and expressions are not supported")


def effective_statement(text, prepared="", depth=0):
    if depth > 8:
        fail("SQL wrapper nesting limit exceeded")
    tokens = sql_access.GatewayTokenizer().tokenize(text)
    if not tokens or tokens[0].text.upper() != "EXECUTE":
        return text
    if len(tokens) < 2:
        fail("Invalid EXECUTE")
    if tokens[1].text.upper() == "IMMEDIATE":
        if len(tokens) < 3 or tokens[2].token_type != TokenType.STRING:
            fail("EXECUTE IMMEDIATE requires a SQL string")
        statement, tail = tokens[2].text, text[tokens[2].end + 1:].strip().rstrip(";")
    else:
        definitions = dict(p.split("=", 1) for p in prepared.split(",") if "=" in p)
        if tokens[1].text not in definitions:
            fail("Prepared statement definition is required")
        statement, tail = unquote_plus(definitions[tokens[1].text]), text[tokens[1].end + 1:].strip().rstrip(";")
    if tail:
        if not re.match(r"(?i)^USING\s", tail):
            fail("Invalid EXECUTE parameters")
        values = parse_one("SELECT " + tail[6:], read="trino").expressions
        placeholders = [t for t in sql_access.GatewayTokenizer().tokenize(statement) if t.text == "?"]
        if len(placeholders) != len(values):
            fail("SQL parameter count does not match")
        for token, value in reversed(list(zip(placeholders, values))):
            statement = statement[:token.start] + value.sql(dialect="trino") + statement[token.end + 1:]
    return effective_statement(statement, prepared, depth + 1)


def selected_row(db, model, where):
    """Precisely identify one row. No unbounded or computed metadata updates."""
    if where is None:
        fail("Metadata UPDATE and DELETE require WHERE identifying one row")
    conditions = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
    clauses, fields = [], set()
    allowed = {c.name for c in model.__table__.columns if c.name in {"id", "resource_id", "app_group_id", "principal_id"}}
    for condition in conditions:
        if (not isinstance(condition, exp.EQ) or not isinstance(condition.this, exp.Column)
                or condition.this.table or condition.this.name not in allowed or condition.this.name in fields):
            fail("Use equality predicates on id or the assignment's resource/recipient IDs")
        fields.add(condition.this.name)
        clauses.append(getattr(model, condition.this.name) == scalar(condition.expression))
    if "id" not in fields and not ({"resource_id", "app_group_id"} <= fields or
                                    {"resource_id", "principal_id"} <= fields or
                                    {"app_group_id", "principal_id"} <= fields):
        fail("Identify one row by id, or both resource/group/principal keys")
    rows = list(db.scalars(select(model).where(*clauses).with_for_update()))
    if len(rows) != 1:
        raise HTTPException(404 if not rows else 422, "Metadata change must identify exactly one existing row")
    return rows[0]


def json_value(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        fail("Use valid JSON for structured metadata fields")


def mutate(db, user, node, table):
    from .routes import rbac as commands
    models = {"app_groups": AppGroup, "identity_bindings": IdentityBinding, "role_assignments": RoleAssignment,
              "service_role_assignments": ServiceRoleAssignment, "catalog_assets": CatalogAsset}
    model = models.get(table)
    if not model:
        fail("This metadata table is read-only. SQL changes support app_groups, identity_bindings, role_assignments, service_role_assignments and catalog_assets (governance/SLA).")
    values, row = {}, None
    if isinstance(node, exp.Insert):
        if any(node.args.get(k) for k in ("conflict", "returning", "with_", "overwrite", "partition", "alternative")):
            fail("Use a single INSERT VALUES without conflict clauses, CTEs or RETURNING")
        target, source = node.this, node.expression
        if not isinstance(target, exp.Schema) or not isinstance(source, exp.Values) or len(source.expressions) != 1:
            fail("Use INSERT INTO table (columns) VALUES (one literal row)")
        names = [c.name for c in target.expressions]
        items = source.expressions[0].expressions
        if len(names) != len(items) or len(set(names)) != len(names):
            fail("INSERT columns must be unique and match the values")
        values = dict(zip(names, map(scalar, items)))
    else:
        if any(node.args.get(k) for k in ("from_", "using", "returning", "with_", "limit", "order", "joins")):
            fail("Metadata mutations do not support joins, CTEs, RETURNING, ordering or limits")
        row = selected_row(db, model, node.args.get("where"))
        if isinstance(node, exp.Update):
            for assignment in node.expressions:
                if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column) or assignment.this.table:
                    fail("Use SET column = literal")
                if assignment.this.name in values:
                    fail("Duplicate SET column")
                values[assignment.this.name] = scalar(assignment.expression)
    deleting = isinstance(node, exp.Delete)
    inserting = isinstance(node, exp.Insert)

    def fields(allowed):
        if set(values) - set(allowed):
            fail("Editable fields for this operation: " + ", ".join(sorted(allowed)))

    if table == "app_groups":
        fields({"name", "description", "owners"})
        if deleting:
            commands.delete_group(row.id, db, user)
        else:
            body = {"name": row.name, "description": row.description, "owners": row.owners} if row else {"owners": [user.principal_id]}
            body.update({k: json_value(v) if k == "owners" else v for k, v in values.items()})
            if inserting:
                commands.create_group(commands.GroupIn(**body), db, user)
            else:
                commands.update_group(row.id, commands.GroupIn(**body), db, user)
    elif table in {"role_assignments", "service_role_assignments"}:
        key = "app_group_id" if table == "role_assignments" else "principal_id"
        fields({"resource_id", key, "role"} if inserting else {"role"})
        if inserting and set(values) != {"resource_id", key, "role"}:
            fail("An assignment requires resource_id, " + key + " and role")
        resource, recipient = (values["resource_id"], values[key]) if inserting else (row.resource_id, getattr(row, key))
        if deleting:
            if key == "app_group_id":
                commands.revoke(resource, recipient, db, user)
            else:
                commands.revoke_service(resource, recipient, db, user)
        else:
            commands.assign(resource, commands.AssignmentIn(**{key: recipient, "role": values.get("role", row.role if row else None)}), db, user)
    elif table == "identity_bindings":
        fields({"app_group_id", "principal_id"})
        if deleting:
            commands.unbind(row.app_group_id, row.principal_id, db, user)
        elif inserting and set(values) == {"app_group_id", "principal_id"}:
            commands.bind(values["app_group_id"], commands.BindingIn(principal_id=values["principal_id"]), db, user)
        else:
            fail("Use INSERT or DELETE to change identity bindings")
    else:
        fields({"governance", "sla"})
        if not isinstance(node, exp.Update) or len(values) != 1:
            fail("Catalog SQL changes support UPDATE of one governance or SLA JSON document")
        from .routes.catalog import Governance, ServiceLevel, governance, sla
        if "governance" in values:
            governance(row.id, Governance(**json_value(values["governance"])), db, user)
        else:
            sla(row.id, ServiceLevel(**json_value(values["sla"])), db, user)
    return 1


def execute(db, who, statement, catalog="", schema="", prepared=""):
    """Return None for engine SQL, otherwise a terminal Trino protocol response."""
    from .routes.common import audit
    from .routes import rbac as commands
    settings = get_settings()
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    try:
        text = effective_statement(statement, prepared)
    except sql_errors.SqlglotError:
        fail("SQL syntax is not supported by the shared gateway")
    # A small, explicit management grammar. Names are quoted; IDs remain stable
    # even if objects are renamed. Native Trino privilege management stays denied.
    grant = re.fullmatch(r'\s*(GRANT|REVOKE)\s+(Owner|Admin|Contributor|Reader|Executor|Consumer)\s+ON\s+RESOURCE\s+\'([^\']+)\'\s+(?:TO|FROM)\s+(GROUP|SERVICE|USER)\s+\'([^\']+)\'\s*;?\s*', text, re.I)
    node = None
    if grant:
        required = "TO" if grant.group(1).upper() == "GRANT" else "FROM"
        if not re.search(r"(?i)\s" + required + r"\s+(GROUP|SERVICE|USER)\s+'", text):
            fail("GRANT uses TO; REVOKE uses FROM")
    if not grant:
        try:
            node = parse_one(text, read="trino")
        except Exception:
            return None  # The normal gateway supplies the syntax error.
        if not isinstance(node, (exp.Insert, exp.Update, exp.Delete)):
            return None
        target = node.this.this if isinstance(node.this, exp.Schema) else node.this
        if not isinstance(target, exp.Table) or (target.catalog or catalog) != settings.metadata_catalog:
            return None
        if (target.db or schema or settings.metadata_schema) != settings.metadata_schema or target.alias or target.args.get("joins"):
            fail("Metadata changes must target the platform schema without aliases")
    if not settings.metadata_sql_enabled:
        raise HTTPException(409, "Platform metadata SQL is not enabled")
    metadata.require(who.user)
    if who.execution:
        raise HTTPException(403, "Platform management SQL requires an interactive Global Admin")
    # Verify one complete statement before calling commands that commit their own
    # audit events. A trailing second statement must never execute the first one.
    from sqlglot import parse
    tokens = sql_access.GatewayTokenizer().tokenize(text)
    if any(t.text == ";" for t in tokens[:-1]):
        fail("Submit one metadata statement at a time")
    if not grant and len(parse(text, read="trino")) != 1:
        fail("Submit one metadata statement at a time")
    try:
        if grant:
            op, role, resource, kind, recipient = grant.groups()
            role = role.capitalize()
            kind = kind.upper()
            if kind == "GROUP":
                group = db.get(AppGroup, recipient) or db.scalar(select(AppGroup).where(AppGroup.name == recipient))
                if not group:
                    fail("App group not found; use its ID or exact name")
                recipient = group.id
            else:
                from .models import DirectoryPrincipal
                principal = db.get(DirectoryPrincipal, recipient)
                if not principal or principal.kind != kind.lower():
                    fail("Choose a directory principal matching SERVICE or USER")
            if op.upper() == "GRANT":
                commands.assign(resource, commands.AssignmentIn(role=role, **{
                    "app_group_id" if kind == "GROUP" else "principal_id": recipient}), db, who.user)
            elif kind == "GROUP":
                existing = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource, RoleAssignment.app_group_id == recipient))
                if existing and existing.role != role:
                    fail("The direct assignment has a different role")
                commands.revoke(resource, recipient, db, who.user)
            else:
                existing = db.scalar(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == resource, ServiceRoleAssignment.principal_id == recipient))
                if existing and existing.role != role:
                    fail("The direct assignment has a different role")
                commands.revoke_service(resource, recipient, db, who.user)
            count = 1
        else:
            count = mutate(db, who.user, node, target.name)
    except (ValidationError, TypeError):
        # Pydantic details can contain sensitive values. Keep the response concise.
        db.rollback()
        fail("Invalid metadata values; use the same fields and values accepted by the UI")
    query = SqlQuery(principal_id=who.user.principal_id, actor=who.user.username, binding=who.binding,
                     initiator_id=who.user.principal_id, state="FINISHED", scope={},
                     expires_at=utcnow() + timedelta(seconds=settings.sql_query_ttl_seconds))
    db.add(query)
    db.flush()
    query.engine_query_id = "fdp_management_" + query.id
    audit(db, who.user.username, "sql.metadata_change", query.id, operation=grant.group(1).upper() if grant else node.key.upper(),
          table="permissions" if grant else target.name, principal_id=who.user.principal_id)
    db.commit()
    return {"id": query.engine_query_id, "stats": {"state": "FINISHED", "queued": False, "scheduled": True,
            "nodes": 0, "totalSplits": 0, "queuedSplits": 0, "runningSplits": 0, "completedSplits": 0,
            "cpuTimeMillis": 0, "wallTimeMillis": 0, "queuedTimeMillis": 0, "elapsedTimeMillis": 0,
            "processedRows": count, "processedBytes": 0, "physicalInputBytes": 0, "peakMemoryBytes": 0, "spilledBytes": 0},
            "columns": [{"name": "rows", "type": "bigint", "typeSignature": {"rawType": "bigint", "arguments": []}}],
            "data": [[count]], "updateType": "METADATA", "updateCount": count, "warnings": []}

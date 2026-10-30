"""Column masks and row filters: who sees which values and rows of a table.

A policy belongs to one table of a governed schema and applies to everyone who queries that
table, except its exempt groups and identities. Policies are resolved per caller when a SQL
query is admitted (like schema grants) and enforced by the layer's Trino through its access
control callbacks, so the SQL editor, apps, dbt, Airflow and the API all get the same result.

Expressions are Trino SQL over the table's own columns. `is_member('<group name>')` is resolved
to TRUE or FALSE for the caller before Trino sees the expression, so one filter can show each
group different rows. Expressions cannot contain subqueries or refer to other tables. They are
stored normalized (parsed and generated again), so the SQL Trino receives is exactly what an
administrator saw, without comments or trailing statements.
"""
from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import select

from .models import AppGroup, DataPolicy, Resource

KINDS = ("column_mask", "row_filter")
METHODS = ("null", "redact", "hash", "partial", "expression")
MAX_EXPRESSION = 4000
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,255}")
_TEXT_TYPE = re.compile(r"(var)?char(\(\d+\))?$")


def normalize_name(value, what):
    """Trino lowercases unquoted identifiers; policies match the names Trino reports."""
    name = (value or "").strip().lower()
    if not _NAME.fullmatch(name):
        raise HTTPException(422, f"{what} must be a lowercase SQL name (letters, digits and underscores)")
    return name


def _parse(expression):
    import logging
    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    text = (expression or "").strip()
    if not text or len(text) > MAX_EXPRESSION:
        raise HTTPException(422, f"an expression of 1 to {MAX_EXPRESSION} characters is required")
    try:
        statements = sqlglot.parse("SELECT " + text, read="trino")
    except sqlglot.errors.SqlglotError:
        raise HTTPException(422, "the expression is not valid Trino SQL") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select) or len(statements[0].expressions) != 1:
        raise HTTPException(422, "use a single SQL expression")
    select_ = statements[0]
    # Nothing but the one expression: no FROM, WHERE, GROUP BY, ... whatever sqlglot calls them.
    if any(value for key, value in select_.args.items() if key != "expressions") or select_.find(exp.Table):
        raise HTTPException(422, "use a single SQL expression")
    node = select_.expressions[0]
    if isinstance(node, exp.Alias):
        raise HTTPException(422, "an expression cannot have an alias")
    for child in node.walk():
        if isinstance(child, (exp.Select, exp.Subquery, exp.Table, exp.Union, exp.Command)):
            raise HTTPException(422, "expressions cannot contain subqueries or refer to other tables")
        if isinstance(child, exp.Anonymous) and child.name.lower() == "is_member":
            if len(child.expressions) != 1 or not isinstance(child.expressions[0], exp.Literal) \
                    or not child.expressions[0].is_string:
                raise HTTPException(422, "is_member takes one group name in quotes")
    return node


def normalize_expression(expression):
    return _parse(expression).sql(dialect="trino", comments=False)


def member_groups(expression):
    from sqlglot import exp
    return sorted({n.expressions[0].this for n in _parse(expression).walk()
                   if isinstance(n, exp.Anonymous) and n.name.lower() == "is_member"})


def render(expression, groups):
    """The expression for one caller: is_member('x') becomes TRUE or FALSE."""
    from sqlglot import exp
    names = {g.lower() for g in groups}

    def resolve(node):
        if isinstance(node, exp.Anonymous) and node.name.lower() == "is_member":
            return exp.true() if node.expressions[0].this.lower() in names else exp.false()
        return node
    return _parse(expression).transform(resolve).sql(dialect="trino", comments=False)


def validate(kind, method, expression, column):
    if kind not in KINDS:
        raise HTTPException(422, "kind is column_mask or row_filter")
    if kind == "row_filter":
        if column:
            raise HTTPException(422, "a row filter applies to the whole table; leave column empty")
        return "", normalize_expression(expression)
    if not column:
        raise HTTPException(422, "a column mask needs a column")
    if method not in METHODS:
        raise HTTPException(422, "method is one of " + ", ".join(METHODS))
    if method == "expression":
        return method, normalize_expression(expression)
    if (expression or "").strip():
        raise HTTPException(422, "only the expression method takes an expression")
    return method, ""


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def mask_sql(method, expression, column, column_type, groups):
    """The Trino expression that replaces the column's values for this caller.

    Text methods (redact, hash, partial) only fit text columns; other types get NULL, which
    still hides the value. The result is cast to the column's own type, as Trino requires.
    """
    kind = (column_type or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_ (),]+", kind):
        kind = "varchar"
    text = bool(_TEXT_TYPE.fullmatch(kind))
    col = _quote(column)
    if method == "expression":
        value = "(" + render(expression, groups) + ")"
    elif method == "redact" and text:
        value = "'****'"
    elif method == "hash" and text:
        value = "to_hex(sha256(to_utf8(%s)))" % col
    elif method == "partial" and text:
        value = ("CASE WHEN %(c)s IS NULL THEN NULL ELSE concat(repeat('*', greatest(length(%(c)s) - 4, 0)), "
                 "substr(%(c)s, greatest(length(%(c)s) - 3, 1))) END") % {"c": col}
    else:
        value = "NULL"
    return "CAST(%s AS %s)" % (value, kind)


def caller_groups(db, user):
    """App group IDs and names of the caller, including nested directory groups."""
    from . import rbac
    ids = rbac.app_groups(db, user)
    names = set(db.scalars(select(AppGroup.name).where(AppGroup.id.in_(ids)))) if ids else set()
    return ids, names


def applies(policy, group_ids, principal_id):
    return not (set(policy.exempt_groups or []) & set(group_ids) or principal_id in (policy.exempt_principals or []))


def attach(db, user, scope):
    """Add the caller's masks and row filters to an admitted SQL scope.

    scope[catalog]["policies"]["<schema>.<table>"] = {"filter": expression or None,
    "masks": {column: {"method", "expression"}}}; only policies that apply to this caller.
    """
    wanted = {}
    for catalog, item in scope.items():
        if item.get("system_metadata"):
            continue
        for schema in item.get("schemas", {}):
            wanted[(item["source_id"], schema)] = catalog
    if not wanted:
        return scope
    resources = {(r.object_id, r.name): r for r in db.scalars(select(Resource).where(
        Resource.kind == "schema", Resource.object_id.in_({s for s, _ in wanted})))}
    by_schema = {resources[key].id: key for key in wanted if key in resources}
    policies = list(db.scalars(select(DataPolicy).where(DataPolicy.schema_id.in_(by_schema)))) if by_schema else []
    if not policies:
        return scope
    group_ids, group_names = caller_groups(db, user)
    for policy in policies:
        if not applies(policy, group_ids, user.principal_id):
            continue
        source_id, schema = by_schema[policy.schema_id]
        catalog = wanted[(source_id, schema)]
        entry = scope[catalog].setdefault("policies", {}).setdefault(
            schema + "." + policy.table_name, {"filter": None, "masks": {}})
        if policy.kind == "row_filter":
            entry["filter"] = render(policy.expression, group_names)
        else:
            entry["masks"][policy.column_name] = {"method": policy.method, "expression": policy.expression}
        scope[catalog]["groups"] = sorted(group_names)
    return scope


def table_policy(query, table):
    """The caller's policies for a table named by Trino, or None."""
    catalog, schema, name = (table.get("catalogName"), table.get("schemaName"), table.get("tableName") or "")
    return (query.scope.get(catalog, {}).get("policies") or {}).get("%s.%s" % (schema, name))


def protected_base(query, table):
    """For Iceberg metadata tables ("orders$files"), the base table if it has policies for this caller.

    These tables expose partition values and column bounds, so masks and filters on the base
    table would not cover them.
    """
    name = table.get("tableName") or ""
    if "$" not in name:
        return None
    return table_policy(query, {**table, "tableName": name.split("$", 1)[0]})


def row_filters(query, table):
    entry = table_policy(query, table)
    return [{"expression": entry["filter"]}] if entry and entry.get("filter") else []


def column_masks(query, columns):
    """Batch masks for Trino: [{"index", "viewExpression": {"expression"}}] for masked columns."""
    result = []
    for index, item in enumerate(columns):
        column = item.get("column") or {}
        entry = table_policy(query, column)
        mask = (entry or {}).get("masks", {}).get((column.get("columnName") or "").lower())
        if mask:
            catalog = column.get("catalogName")
            groups = query.scope.get(catalog, {}).get("groups", [])
            result.append({"index": index, "viewExpression": {"expression": mask_sql(
                mask["method"], mask["expression"], column["columnName"], column.get("columnType"), groups)}})
    return result


def schema_has_policies(db, schema_ids):
    return bool(schema_ids) and db.scalar(select(DataPolicy.id).where(DataPolicy.schema_id.in_(schema_ids)).limit(1)) is not None

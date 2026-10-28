"""Native catalog search: structured expressions, weighted fields, prefixes and typo tolerance."""
import fnmatch
import re
import unicodedata
from difflib import SequenceMatcher

from fastapi import HTTPException
from sqlalchemy import text, or_

from .models import AppGroup, CatalogAsset

FIELDS = {"name", "description", "domain", "tag", "owner", "kind", "classification", "status", "branch", "platform", "path", "column", "layer"}
ALIASES = {"type": "kind", "tags": "tag", "columns": "column", "environment": "layer", "container": "path"}
WEIGHTS = {"name": 100, "column": 70, "tag": 70, "path": 45, "domain": 40, "owner": 35,
           "description": 20, "kind": 20, "platform": 20, "branch": 20, "status": 10, "classification": 10, "layer": 25}
TOKEN = re.compile(r'\(|\)|-?(?:[\w]+:)?(?:"[^"\n]*"|[^\s()]+)')


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)).casefold() if not unicodedata.combining(c))


def parse(query):
    query = query.removeprefix("/q ").strip()
    if query.count('"') % 2:
        raise HTTPException(422, "Close the quoted phrase in your search.")
    tokens = TOKEN.findall(query)
    if len(tokens) > 64:
        raise HTTPException(422, "Use at most 64 search terms/operators.")
    index = 0

    def expression(depth=0):
        nonlocal index
        if depth > 12:
            raise HTTPException(422, "Search nesting exceeds 12 levels.")
        clauses = [conjunction(depth)]
        while index < len(tokens) and tokens[index] in ("OR", "|"):
            index += 1
            clauses.append(conjunction(depth))
        return ("or", clauses) if len(clauses) > 1 else clauses[0]

    def conjunction(depth):
        nonlocal index
        clauses = []
        while index < len(tokens) and tokens[index] not in ("OR", "|", ")"):
            if tokens[index] in ("AND", "+"):
                if not clauses:
                    raise HTTPException(422, "AND needs a term on each side.")
                index += 1
                if index >= len(tokens) or tokens[index] in ("OR", "|", ")"):
                    raise HTTPException(422, "AND needs a term on each side.")
            clauses.append(atom(depth))
        if not clauses:
            raise HTTPException(422, "Add a search term after the operator.")
        return ("and", clauses) if len(clauses) > 1 else clauses[0]

    def atom(depth):
        nonlocal index
        token = tokens[index]; index += 1
        if token == "NOT" or token == "-":
            if index >= len(tokens):
                raise HTTPException(422, "NOT needs a search term.")
            return ("not", atom(depth + 1))
        if token == "(":
            value = expression(depth + 1)
            if index >= len(tokens) or tokens[index] != ")":
                raise HTTPException(422, "Close the search parentheses.")
            index += 1
            return value
        if token.startswith("-"):
            token = token[1:]
            negative = True
        else:
            negative = False
        field, separator, value = token.partition(":")
        if separator:
            field = ALIASES.get(field.casefold(), field.casefold())
            if field not in FIELDS:
                raise HTTPException(422, "Unknown search field. Use name, type, domain, tag, owner, column, platform, branch, path, status or classification.")
            if not value and index < len(tokens):
                value = tokens[index]; index += 1
            if not value:
                raise HTTPException(422, "A field filter needs a value.")
        else:
            field, value = None, token
        quoted = value.startswith('"') and value.endswith('"')
        value = normalize(value.strip('"'))
        if not value:
            raise HTTPException(422, "Search terms cannot be empty.")
        result = ("term", field, value, quoted)
        return ("not", result) if negative else result

    if not tokens:
        return None
    result = expression()
    if index != len(tokens):
        raise HTTPException(422, "Unexpected search parenthesis.")
    return result


def document(asset, groups):
    g, a = asset.governance or {}, asset.attributes or {}
    return {"layer": [getattr(asset, "catalog_layer", {}).get("layer_name", ""), getattr(asset, "catalog_layer", {}).get("layer_id", "")], "name": [asset.name], "path": [asset.path], "kind": [asset.kind],
        "description": [g.get("description") or a.get("description", ""), *[col.get("description", "") for col in a.get("columns", [])]],
        "domain": [g.get("domain", "")], "tag": g.get("tags", []),
        "owner": [groups.get(g.get("owner_group_id"), ""), g.get("steward", "")],
        "column": [col.get("name", "") for col in a.get("columns", [])],
        "branch": [a.get("branch", "")], "platform": [a.get("source_kind", ""), a.get("source", ""), a.get("repo", "")],
        "status": [g.get("status", "draft")], "classification": [g.get("classification", "unclassified")]}


def evaluate(tree, doc, fuzzy=False):
    if tree is None:
        return (0.0, set(), False)
    op = tree[0]
    if op == "not":
        return (0.0, set(), False) if evaluate(tree[1], doc, False) is None else None
    if op in ("and", "or"):
        values = [evaluate(child, doc, fuzzy) for child in tree[1]]
        if op == "and":
            if any(v is None for v in values):
                return None
            return (sum(v[0] for v in values), set().union(*(v[1] for v in values)), any(v[2] for v in values))
        matches = [v for v in values if v is not None]
        return max(matches, key=lambda v: v[0]) if matches else None
    _, field, term, quoted = tree
    best = None
    for key in ([field] if field else WEIGHTS):
        for raw in doc.get(key, []):
            value = normalize(raw)
            if not value:
                continue
            score, approximate = 0, False
            if term == "*":
                score = 1
            elif "*" in term or "?" in term:
                if fnmatch.fnmatchcase(value, term) or any(fnmatch.fnmatchcase(word, term) for word in re.findall(r"[^\W_]+", value)):
                    score = 1
            elif term == value:
                score = 2
            elif quoted:
                if term in value:
                    score = 1.5
            elif field in {"kind", "domain", "tag", "branch", "status", "classification"}:
                pass  # Structured facet values are exact unless a wildcard is explicit.
            elif term in value:
                score = 1.5 if value.startswith(term) else 1
            elif fuzzy and field is None and len(term) >= 4:
                for word in re.findall(r"[^\W_]+", value):
                    if abs(len(word) - len(term)) <= 2 and SequenceMatcher(None, term, word, autojunk=False).ratio() >= .82:
                        score, approximate = .55, True
                        break
            if score:
                result = (score * WEIGHTS[key], {key}, approximate)
                if best is None or result[0] > best[0]:
                    best = result
    return best


def ranked(assets, query, groups, typo_tolerance=True):
    tree = parse(query)
    matches = []
    phrase = normalize(query.strip('" '))
    for asset in assets:
        doc = document(asset, groups)
        result = evaluate(tree, doc, typo_tolerance)
        if result is not None:
            score, fields, approximate = result
            if phrase and normalize(asset.name) == phrase:
                score += 200
            matches.append((asset, score, sorted(fields), approximate))
    matches.sort(key=lambda m: (-m[1], normalize(m[0].name), m[0].id))
    return matches


def suggestions(assets, query, groups, limit=8):
    # Suggest only names from the already authorized set, never a global vocabulary.
    matches = ranked(assets, query, groups)
    return [{"id": asset.id, "name": asset.name, "kind": asset.kind, "path": asset.path,
             "approximate": approximate} for asset, _, _, approximate in matches[:limit]]


def fulltext_condition(db, query, groups=None):
    """Indexed candidate selection; exact expression evaluation still applies afterwards."""
    tree = parse(query)
    dialect = db.bind.dialect.name
    if not tree or dialect not in ("postgresql", "sqlite"):
        return None

    def build(node):
        op = node[0]
        if op == "not":
            return None
        if op in ("and", "or"):
            children = [build(child) for child in node[1]]
            if op == "or" and any(child is None for child in children):
                return None
            children = [child for child in children if child]
            operator = (" & " if op == "and" else " | ") if dialect == "postgresql" else (" AND " if op == "and" else " OR ")
            return "(" + operator.join(children) + ")" if children else None
        _, field, term, quoted = node
        if field or term.startswith("*") or "?" in term:
            return None
        words = re.findall(r"[^\W_]+", term)
        if not words:
            return None
        if dialect == "postgresql":
            return "(" + " & ".join("'" + word + "':*" for word in words) + ")"
        return "(" + " AND ".join('"' + word + '"*' for word in words) + ")"

    expression = build(tree)
    if not expression:
        return None
    if dialect == "postgresql":
        condition = text("to_tsvector('simple', coalesce(catalog_assets.search_text, '')) @@ to_tsquery('simple', :catalog_query)").bindparams(catalog_query=expression)
    else:
        condition = CatalogAsset.id.in_(text("SELECT asset_id FROM catalog_search_fts WHERE catalog_search_fts MATCH :catalog_query").bindparams(catalog_query=expression))
    # Group names can change without touching every owned asset. Include candidate
    # owners by their current name, then evaluate the complete expression as usual.
    def owner_terms(node):
        if node[0] in ("and", "or"):
            return [term for child in node[1] for term in owner_terms(child)]
        return [node[2]] if node[0] == "term" and node[1] is None else []
    terms = owner_terms(tree)
    owners = [ident for ident, name in (groups or {}).items() if any(term in normalize(name) for term in terms)]
    return or_(condition, CatalogAsset.governance["owner_group_id"].as_string().in_(owners)) if owners else condition


def ensure_index(engine):
    """Database-native full-text index, with no additional search service."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS catalog_fulltext_idx ON catalog_assets USING gin(to_tsvector('simple', coalesce(search_text, '')))"))
    elif engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search_fts USING fts5(asset_id UNINDEXED, search_text, tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')"))
            conn.execute(text("CREATE TRIGGER IF NOT EXISTS catalog_fts_insert AFTER INSERT ON catalog_assets BEGIN INSERT INTO catalog_search_fts(asset_id, search_text) VALUES (new.id, new.search_text); END"))
            conn.execute(text("CREATE TRIGGER IF NOT EXISTS catalog_fts_update AFTER UPDATE OF search_text ON catalog_assets WHEN old.search_text IS NOT new.search_text BEGIN DELETE FROM catalog_search_fts WHERE asset_id = old.id; INSERT INTO catalog_search_fts(asset_id, search_text) VALUES (new.id, new.search_text); END"))
            conn.execute(text("CREATE TRIGGER IF NOT EXISTS catalog_fts_delete AFTER DELETE ON catalog_assets BEGIN DELETE FROM catalog_search_fts WHERE asset_id = old.id; END"))
            conn.execute(text("DELETE FROM catalog_search_fts"))
            conn.execute(text("INSERT INTO catalog_search_fts(asset_id,search_text) SELECT id,search_text FROM catalog_assets"))

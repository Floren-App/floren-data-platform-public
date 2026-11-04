"""Generate Markdown references using isolated settings, never an installed platform."""
import ast
import json
import os
from pathlib import Path
import sys
import warnings
import re

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reference'


def route_inventory(app):
    """Include mounted native aliases and callbacks, even when excluded from OpenAPI."""
    lines = ['# Route and page inventory\n\nGenerated from mounted FastAPI routes and desktop page mappings. '
        'Entry-point inventory is not proof of authorization coverage; consult the '
        '[matrix](permission-matrix.md) and [migration status](scoped-permissions.md).\n\n## HTTP entry points\n']
    groups = {}
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        module = getattr(endpoint, '__module__', '')
        if not module.startswith('app.'):
            continue
        methods = sorted(getattr(route, 'methods', set()) - {'HEAD', 'OPTIONS'})
        if methods:
            groups.setdefault(module.rsplit('.', 1)[-1], []).append((route, endpoint, module, methods))
    for group, entries in sorted(groups.items()):
        lines += [f'\n### {group}\n\n| Method | Path | Handler / source |\n|---|---|---|\n']
        for route, endpoint, module, methods in sorted(entries, key=lambda entry:(entry[0].path, entry[3])):
            source = module.replace('.', '/') + '.py'
            code = getattr(endpoint, '__code__', None)
            anchor = '#L'+str(code.co_firstlineno) if code else ''
            lines.append(f'| {", ".join(methods)} | `{route.path}` | '
                f'[`{endpoint.__name__}`](../services/control-plane/{source}{anchor}) |\n')
    lines += ['\n## Desktop pages and aliases\n\nEvery tab below requires its page gate before mounting, except reference pages marked none. '
        'Page visibility and object/function authority are separate.\n\n| Tab kind | Page gate |\n|---|---|\n']
    mapping = (ROOT/'apps/desktop/src/permissions.ts').read_text().split('export const TAB_PAGES', 1)[1].split('};', 1)[0]
    for tab, page in re.findall(r'"([a-z-]+)":\s*"([a-z-]+)"', mapping):
        lines.append(f'| `{tab}` | `{page}` |\n')
    for tab in re.findall(r'"([a-z-]+)":\s*null', mapping):
        lines.append(f'| `{tab}` | none: a reference page for everyone signed in |\n')
    lines += ['\n## Other entry points\n\nGateway forward authentication, broker policy callbacks, SQL policy callbacks, '
        'native package indexes and keyed aliases are included above. They require their '
        'own admission, service authentication and live object checks. Framework documentation '
        'routes, startup/leader controllers and offline recovery/migration commands are additional '
        'boundaries; controller entry points must preserve the authority captured at admission.\n']
    (ROOT/'docs/permission-route-inventory.md').write_text(''.join(lines))


def generate():
    # Imports construct an unconnected in-memory engine; app lifespan is never started.
    os.environ.update(FDP_DATABASE_URL='sqlite://', FDP_ORCHESTRATOR='fake',
                      FDP_SECRETS_BACKEND='memory', FDP_SESSION_SIGNING_KEY='docs-only-not-a-runtime-signing-key',
                      FDP_GATEWAY_CONFIG_TOKEN='docs-only-not-a-runtime-token')
    sys.path.insert(0, str(ROOT / 'services/control-plane'))
    from app.main import app
    from app.programs import json_schema
    OUT.mkdir(parents=True, exist_ok=True)
    route_inventory(app)
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Duplicate Operation ID .*', category=UserWarning)
        spec = app.openapi()
    # FastAPI multi-method routes otherwise reuse operation IDs. Give the exported
    # reference deterministic, unique IDs without modifying the running application.
    for path, methods in spec['paths'].items():
        for method, operation in methods.items():
            if isinstance(operation, dict) and 'operationId' in operation:
                operation['operationId'] = method + '_' + path.strip('/').replace('/', '_').replace('{', '').replace('}', '')
    (OUT / 'openapi.json').write_text(json.dumps(spec, indent=2, sort_keys=True) + '\n')
    (OUT / 'fdp.schema.json').write_text(json.dumps(json_schema(), indent=2, sort_keys=True) + '\n')
    pages = ['# HTTP API reference\n\nGenerated from FastAPI OpenAPI; do not edit manually.\n\n'
             'Download the [OpenAPI specification](openapi.json). Paths below are relative to the '
             'public `/api` prefix. Authenticate with an IdP bearer token or an authorized FDP execution token. '
             'SCIM uses its separate provisioning credential. Internal endpoints are not a supported client API.\n\n'
             'The installed server also exposes interactive OpenAPI at `/api/docs` when routed by its gateway. '
             'Use its schema to inspect request/response models for that installed revision.\n']
    groups = {}
    for path, methods in sorted(spec['paths'].items()):
        if not path.startswith(('/v1/', '/scim/')):
            continue
        for method, operation in methods.items():
            if method not in {'get', 'post', 'put', 'patch', 'delete'}:
                continue
            tag = operation.get('tags', ['Other'])[0]
            groups.setdefault(tag, []).append((path, method, operation))
    for tag, operations in sorted(groups.items()):
        pages += ['\n## ' + tag + '\n\n', '| Method | Path | Purpose |\n|---|---|---|\n']
        for path, method, op in operations:
            purpose = op.get('summary', '').replace('|', '\\|').replace('\n', ' ')
            pages.append(f'| `{method.upper()}` | `{path}` | {purpose} |\n')
    (OUT / 'api.md').write_text(''.join(pages))
    (OUT / 'json-schema.md').write_text('''# JSON configuration schema

Generated schemas describe accepted configuration; server-side authorization and
layer restrictions still apply.

- [Download the fdp.json schema](fdp.schema.json).
- [Readable program configuration guide](../fdp-json.md).
- [HTTP OpenAPI specification](openapi.json), including workload and infrastructure models.

VS Code's FDP extension includes the program schema. Keep it synchronized using
`make schema` when changing the configuration model. Documentation generation does
not connect to the metadata database, IdP, Kubernetes or any data source.
''')
    source = ROOT / 'sdk/python/fdp/__init__.py'
    tree = ast.parse(source.read_text())
    exports = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '__all__' for t in n.targets))
    lines = ['# Python function reference\n\nGenerated from public SDK function signatures and docstrings. '
             'See the [Python guide](../python-sdk.md) for authentication and examples.\n']
    quality = ast.parse((source.parent / 'quality.py').read_text())
    for node in [*tree.body, *quality.body]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in exports:
            signature = f'{node.name}({ast.unparse(node.args)})'
            lines += [f'\n## `{node.name}`\n\n```python\n{signature}\n```\n\n', (ast.get_docstring(node) or 'See the source for details.') + '\n']
    (OUT / 'python.md').write_text(''.join(lines))
    print('Generated HTTP API, program schema and Python references (no runtime connections).')


if __name__ == '__main__':
    generate()

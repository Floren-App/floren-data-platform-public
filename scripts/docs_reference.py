"""Generate Markdown references using isolated settings, never an installed platform."""
import ast
import json
import os
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reference'


def generate():
    # Imports construct an unconnected in-memory engine; app lifespan is never started.
    os.environ.update(FDP_DATABASE_URL='sqlite://', FDP_ORCHESTRATOR='fake',
                      FDP_SECRETS_BACKEND='memory', FDP_SESSION_SIGNING_KEY='docs-only-not-a-runtime-signing-key',
                      FDP_GATEWAY_CONFIG_TOKEN='docs-only-not-a-runtime-token')
    sys.path.insert(0, str(ROOT / 'services/control-plane'))
    from app.main import app
    from app.programs import json_schema
    OUT.mkdir(parents=True, exist_ok=True)
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

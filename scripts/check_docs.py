"""Check the built site, including search, rather than trusting navigation alone."""
import json
from pathlib import Path
import re
import sys
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {'href', 'src'} and value:
                self.urls.append(value)


def check(site):
    site = Path(site)
    assert (site / 'index.html').is_file(), 'Build the documentation first'
    index = json.loads((site / 'search/search_index.json').read_text())
    assert len(index['docs']) > 30, 'Search index is unexpectedly empty'
    prohibited = [r'tail[a-z0-9]+\.ts\.net', r'/Users/[^\s<>]+',
                  r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                  r'[?&](?:dc_ticket|dc_token)=']
    for path in site.rglob('*'):
        if path.suffix not in {'.html', '.json', '.xml'}:
            continue
        text = path.read_text()
        for pattern in prohibited:
            assert not re.search(pattern, text), f'Private/runtime content in {path.relative_to(site)}'
        if path.suffix == '.html':
            refs = References()
            refs.feed(text)
            for ref in refs.urls:
                url = urlsplit(ref)
                if url.scheme or url.netloc or not url.path or url.path.startswith('/'):
                    continue
                target = (path.parent / unquote(url.path)).resolve()
                assert target.exists(), f'Missing link/asset from {path.relative_to(site)}: {ref}'
    locations = [item['location'] for item in index['docs']]
    assert not any('progress' in p or '/verification/' in p or 'rbac-plan' in p for p in locations)
    for route in ['workspaces', 'rbac', 'catalog', 'terraform', 'pokemon', 'troubleshooting', 'reference/api',
                  'compute', 'configuration', 'deploying', 'api-management', 'packages', 'storage-integrations']:
        assert (site / route / 'index.html').exists(), f'Missing app/help route: {route}'
    print(f'Documentation checks passed: {len(locations)} searchable sections; no runtime material detected.')


if __name__ == '__main__':
    check(sys.argv[1] if len(sys.argv) > 1 else 'site')

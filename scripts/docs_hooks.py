"""Only explicitly published pages enter the site, including its search index."""
from pathlib import Path
import re
from urllib.parse import urlsplit

from mkdocs.structure.files import Files


def _pages(nav):
    for item in nav:
        for value in item.values():
            if isinstance(value, list):
                yield from _pages(value)
            elif isinstance(value, str):
                yield value


def on_config(config):
    # Generated provider pages stay in navigation and search without hand-maintained lists.
    root = Path(config['docs_dir']) / 'reference/terraform'
    generated = [{p.stem: 'reference/terraform/' + p.name}
                 for p in sorted(root.glob('*.md')) if p.name != 'index.md']
    for section in config['nav']:
        if 'Reference' in section:
            section['Reference'].append({'Terraform resources': generated})
    return config


def on_files(files, config):
    public = set(_pages(config['nav'])) | {'reference/openapi.json', 'reference/fdp.schema.json', 'permission-matrix.csv'}
    # Do not publish progress logs, verification evidence, scripts or arbitrary new files.
    # Theme/plugin assets are not authored docs and must remain available for
    # rendering and the browser search worker. Only docs_dir is allowlisted.
    return Files(f for f in files if f.src_uri in public or f.src_dir != config['docs_dir'])


def on_page_markdown(markdown, page, config, **kwargs):
    """Repository source links keep working both in Markdown and on the static site."""
    docs = Path(config['docs_dir']).resolve()
    repo = docs.parent
    source = config['repo_url'].rstrip('/') + '/blob/main/'
    def rewrite(match):
        target = match.group(1)
        if urlsplit(target).scheme or target.startswith(('#', '/')):
            return match.group(0)
        path, _, fragment = target.partition('#')
        resolved = (docs / page.file.src_uri).parent.joinpath(path).resolve()
        if resolved.is_relative_to(docs):
            return match.group(0)
        if not resolved.is_relative_to(repo) or not resolved.exists():
            raise ValueError(f'{page.file.src_uri}: invalid source link {target}')
        relative = resolved.relative_to(repo).as_posix()
        return '](' + source + relative + ('#' + fragment if fragment else '') + ')'
    return re.sub(r'\]\(([^\s)]+)\)', rewrite, markdown)

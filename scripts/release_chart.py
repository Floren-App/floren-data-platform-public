#!/usr/bin/env python3
"""Prepare the Helm chart for a release: chart version, and every platform image pinned to the release's images.

    python3 scripts/release_chart.py 0.3.0 [--registry ghcr.io/floren-app] [--chart deploy/helm/fdp]

Third-party images (Keycloak, PostgreSQL, Trino, OpenBao, ...) keep their pinned versions. Edits in place,
preserving comments; refuses versions that are not semantic versions.
"""
import argparse
import re
import sys
from pathlib import Path


def release(version: str, registry: str, chart: Path) -> None:
    if not re.fullmatch(r'\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?', version):
        sys.exit(f'not a semantic version: {version}')
    meta = chart / 'Chart.yaml'
    text = meta.read_text()
    text = re.sub(r'(?m)^version: .*$', f'version: {version}', text)
    text = re.sub(r'(?m)^appVersion: .*$', f'appVersion: "{version}"', text)
    meta.write_text(text)
    values = chart / 'values.yaml'
    text = values.read_text()
    text, control_plane = re.subn(r'(?m)^(image:\n  repository: )fdp/control-plane\n  tag: "[^"]*"',
                                  rf'\1{registry}/fdp-control-plane\n  tag: "{version}"', text)
    # fdp/<image>:<tag> -> <registry>/fdp-<image>:<version>, wherever a platform image is referenced.
    text, images = re.subn(r'(?<![\w./-])fdp/([a-z][a-z0-9-]*):[^\s#"]+', rf'{registry}/fdp-\1:{version}', text)
    if control_plane != 1 or images < 8:
        sys.exit(f'unexpected values layout (control plane {control_plane}, images {images}); update this script')
    values.write_text(text)
    print(f'chart {version}: control plane and {images} platform images from {registry}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('version')
    parser.add_argument('--registry', default='ghcr.io/floren-app')
    parser.add_argument('--chart', type=Path, default=Path('deploy/helm/fdp'))
    args = parser.parse_args()
    release(args.version, args.registry.rstrip('/'), args.chart)

#!/usr/bin/env python3
"""Verify an exported FDP audit trail: hash chain continuity, event integrity and (optionally) signatures.

Reads JSON lines (one event per line) from files or stdin, as received from one SIEM destination: the generic or
stdout format, Splunk HEC events (``{"event": {...}}``) and Elastic documents (``@timestamp`` is ignored) are all
accepted. Duplicate deliveries of the same event are fine. Set FDP_SIEM_SIGNING_KEY to also check signatures.

    python3 scripts/verify-audit-chain.py export.ndjson
    FDP_SIEM_SIGNING_KEY=... python3 scripts/verify-audit-chain.py < export.ndjson

Exit status 0 when the chain is intact, 1 otherwise. Uses only the Python standard library.
"""
import hashlib
import hmac
import json
import os
import sys


def canonical(event):
    return json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()


def events(lines):
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if 'event' in value and isinstance(value['event'], dict):
            value = value['event']
        if value.get('schema', '').startswith('fdp.audit/'):
            value.pop('@timestamp', None)
            yield number, value


def verify(lines, key=None):
    problems, seen, previous = [], {}, None
    for number, event in sorted(events(lines), key=lambda item: item[1]['chain']['seq']):
        chain, signature = event.pop('chain'), event.pop('signature', None)
        seq = chain['seq']
        if seq in seen:
            if seen[seq] != chain['hash']:
                problems.append(f'line {number}: sequence {seq} delivered twice with different content')
            continue
        digest = hashlib.sha256(chain['prev'].encode() + canonical(event)).hexdigest()
        if digest != chain['hash']:
            problems.append(f'line {number}: event {event.get("id")} was altered (hash mismatch)')
        if previous is not None:
            if seq != previous[0] + 1:
                problems.append(f'line {number}: events {previous[0] + 1}..{seq - 1} are missing')
            elif chain['prev'] != previous[1]:
                problems.append(f'line {number}: chain broken before event {event.get("id")}')
        if key is not None:
            expected = hmac.new(key.encode(), chain['hash'].encode(), hashlib.sha256).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
                problems.append(f'line {number}: event {event.get("id")} has an invalid signature')
        seen[seq] = chain['hash']
        previous = (seq, chain['hash'])
    return len(seen), problems


def main():
    sources = [open(path, encoding='utf-8') for path in sys.argv[1:]] or [sys.stdin]
    lines = [line for source in sources for line in source]
    count, problems = verify(lines, os.environ.get('FDP_SIEM_SIGNING_KEY'))
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f'{count} events checked: ' + ('chain intact' if not problems else f'{len(problems)} problem(s)'))
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()

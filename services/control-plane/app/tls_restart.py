"""Sidecar for servers that read their certificate only at startup (RustFS).

Watches the projected TLS Secret for a new revision and stops the server process so that the
kubelet restarts that container with the renewed certificate. Needs a shared process namespace
and the same user as the server; it has no Kubernetes access and never reads the key.

    python -m app.tls_restart --watch /tls --process rustfs
"""
import argparse
import os
import signal
import time
from pathlib import Path


def revision(directory: str) -> str:
    """Kubelet swaps the ..data symlink atomically on every Secret update."""
    try:
        return os.readlink(os.path.join(directory, '..data'))
    except OSError:
        return ''


def processes(name: str, proc: str = '/proc'):
    found = []
    for entry in Path(proc).iterdir():
        if entry.name.isdigit():
            try:
                if (entry / 'comm').read_text().strip() == name:
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def restart(name: str, proc: str = '/proc', kill=os.kill) -> int:
    pids = processes(name, proc)
    for pid in pids:
        kill(pid, signal.SIGTERM)
    return len(pids)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', required=True)
    parser.add_argument('--process', required=True)
    parser.add_argument('--interval', type=float, default=10)
    args = parser.parse_args(argv)
    current = revision(args.watch)
    while True:
        time.sleep(args.interval)
        latest = revision(args.watch)
        if latest and latest != current:
            stopped = restart(args.process)
            print(f'certificate changed; restarted {args.process} ({stopped} process)', flush=True)
            current = latest


if __name__ == '__main__':
    main()

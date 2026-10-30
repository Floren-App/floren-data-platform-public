"""Supervise a continuous program and expose readiness only after it announces ready."""
import http.server
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading


def ready():
    """Call after a streaming client is initialized; Kafka SDK factories do this automatically."""
    path = os.environ.get('FDP_STREAM_READY_FILE')
    if path:
        Path(path).touch()


def main():
    command = sys.argv[1:]
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        raise SystemExit('Usage: python -m fdp.streaming -- <command>')
    fd, marker = tempfile.mkstemp(prefix='fdp-stream-')
    os.close(fd)
    Path(marker).unlink()
    child = subprocess.Popen(command, env={**os.environ,'FDP_STREAM_READY_FILE':marker})
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == '/healthz' and child.poll() is None and Path(marker).is_file()
            self.send_response(200 if ok else 503)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = None
    killer = None
    def stop(*_):
        nonlocal killer
        child.terminate()
        if killer is None:
            killer = threading.Timer(20, lambda: child.kill() if child.poll() is None else None)
            killer.daemon = True
            killer.start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        port = int(os.environ.get('FDP_STREAM_HEALTH_PORT', '8501'))
        server = http.server.ThreadingHTTPServer(('0.0.0.0',port), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        code = child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if killer:
            killer.cancel()
        if server:
            server.shutdown()
            server.server_close()
        Path(marker).unlink(missing_ok=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()

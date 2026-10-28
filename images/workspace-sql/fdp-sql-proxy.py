"""Local Trino endpoint for the SQL workspace: adds this workspace's Floren Data Platform identity to every request.

CloudBeaver's Trino JDBC connection points at http://127.0.0.1:8099 without credentials, so no token is
stored in CloudBeaver's configuration, and Trino's JDBC driver never sends credentials over plain HTTP.
This process forwards only the Trino client protocol (statements, result pages, info) to the Floren Data Platform
SQL gateway with the workspace token, and rewrites result-page links back to itself. Governance, auditing
and row limits all stay in the gateway and the shared engine.
"""
import http.server
import os
import socketserver
import urllib.error
import urllib.request

UPSTREAM = os.environ["FDP_API"].rstrip("/")
LISTEN = int(os.environ.get("FDP_SQL_PROXY_PORT", "8099"))
LOCAL = f"http://127.0.0.1:{LISTEN}"
ALLOWED = ("/v1/statement", "/v1/sql/pages/", "/v1/sql/queries/", "/v1/info")
FORWARD = {"content-type", "accept", "accept-encoding", "user-agent"}  # plus every X-Trino-* header
RETURN = {"content-type"}                                                # plus every X-Trino-* header


def token() -> str:
    return os.environ["FDP_SESSION_TOKEN"]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # result pages are frequent; errors are logged below
        pass

    def forward(self):
        if not self.path.startswith(ALLOWED):
            self.send_error(404, "only the Trino client protocol is available here")
            return
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() in FORWARD or k.lower().startswith("x-trino-")}
        headers["Authorization"] = "Bearer " + token()
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                status, reply, payload = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            status, reply, payload = error.code, error.headers, error.read()
            self.log_error("gateway returned %s for %s %s", status, self.command, self.path.split("?")[0])
        except OSError as error:
            self.send_error(502, f"Floren Data Platform SQL gateway unreachable: {error}")
            return
        # Continuation links point at the gateway; keep the client on this endpoint.
        payload = payload.replace(UPSTREAM.encode() + b"/v1/", LOCAL.encode() + b"/v1/")
        self.send_response(status)
        for key, value in reply.items():
            if key.lower() in RETURN or key.lower().startswith("x-trino-"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_HEAD = forward


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    Server(("127.0.0.1", LISTEN), Handler).serve_forever()

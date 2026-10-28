"""Execution images trust a private CA only through the platform bundle the orchestrator mounts.

Runs the R compute image and the RStudio and VS Code workspace images against an HTTPS server whose certificate
comes from a throwaway private CA. Each client must succeed with the bundle mounted and the
orchestrator's trust variables set, and refuse without them. The RStudio check runs R with an
empty environment, as rsession starts it, so the variables must reach it through Renviron.

    make test-image-trust
"""
import shutil
import ssl
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.orchestrator.kubernetes import TRUST_DIR, TRUST_ENV  # noqa: E402

BUNDLE = f"{TRUST_DIR}/ca-bundle.pem"
R_IMAGE = "fdp/compute-r:dev"
RSTUDIO_IMAGE = "fdp/workspace-rstudio:dev"
VSCODE_IMAGE = "fdp/workspace-vscode:dev"
SERVER_IMAGE = "fdp/compute-python:dev"
R_CLIENTS = """u <- "https://tlsserver:8443/"
f <- function(l, e) cat(l, tryCatch(e, error = function(x) "REFUSED"), "\\n", sep = "")
f("httr2=", httr2::resp_body_string(httr2::req_perform(httr2::request(u))))
f("curl=", rawToChar(curl::curl_fetch_memory(u)$content))
f("download.file=", { t <- tempfile(); download.file(u, t, quiet = TRUE); readLines(t, warn = FALSE) })"""
EDITOR_CLIENTS = """
python3 -c "import urllib.request as u; print('python=' + u.urlopen('https://tlsserver:8443/').read().decode())" 2>/dev/null || echo python=REFUSED
/usr/lib/code-server/lib/node -e "require('https').get('https://tlsserver:8443/', r => r.on('data', d => console.log('node=' + d))).on('error', () => console.log('node=REFUSED'))"
"""
SERVER = """import http.server, ssl
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Length', '7'); self.end_headers(); self.wfile.write(b'trusted')
s = http.server.HTTPServer(('0.0.0.0', 8443), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain('/t/leaf.pem', '/t/leaf.key')
s.socket = ctx.wrap_socket(s.socket, server_side=True); s.serve_forever()
"""


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise SystemExit(f"{' '.join(args[:4])}... failed: {result.stderr[-500:]}")
    return result.stdout


def certificates(directory: Path):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc)

    def build(subject, issuer, public_key, signer, ca, names=()):
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(hours=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if names:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        return builder.sign(signer, hashes.SHA256())

    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Execution Trust Test Root")])
    ca = build(ca_name, ca_name, ca_key.public_key(), ca_key, True)
    leaf = build(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tlsserver")]), ca_name,
                 leaf_key.public_key(), ca_key, False, ["tlsserver"])
    pem = serialization.Encoding.PEM
    (directory / "leaf.pem").write_bytes(leaf.public_bytes(pem))
    (directory / "leaf.key").write_bytes(leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                                serialization.NoEncryption()))
    # Like a real platform bundle: the public roots plus the private CA, and no private keys.
    public_roots = Path(ssl.get_default_verify_paths().cafile or "/etc/ssl/cert.pem").read_text()
    (directory / "bundle.pem").write_text(public_roots + ca.public_bytes(pem).decode())


def main():
    work = Path(tempfile.mkdtemp(prefix="image-trust-"))
    network, server = f"trust-{uuid.uuid4().hex[:6]}", "tlsserver"
    trust_env = [arg for name in TRUST_ENV for arg in ("-e", f"{name}={BUNDLE}")]
    mount = ["-v", f"{work / 'bundle.pem'}:{BUNDLE}:ro"]
    failures = []

    def expect(label, output, trusted):
        lines = [line for line in output.splitlines() if "=" in line]
        for line in lines:
            client, result = line.split("=", 1)
            ok = (result == "trusted") if trusted else (result == "REFUSED")
            print(f"  {'ok ' if ok else 'FAIL'} {label}: {client} {result}")
            if not ok:
                failures.append(f"{label}: {client}")
        if not lines:
            failures.append(f"{label}: no output")

    try:
        certificates(work)
        (work / "serve.py").write_text(SERVER)
        run("docker", "network", "create", network)
        run("docker", "run", "-d", "--rm", "--name", f"{network}-server", "--network-alias", server, "--network", network,
            "-v", f"{work}:/t:ro", SERVER_IMAGE, "python", "/t/serve.py")
        git = 'o=$(git ls-remote https://tlsserver:8443/x 2>&1); ' \
              'case "$o" in *certificate*) echo git=REFUSED;; *) echo git=trusted;; esac'
        for trusted in (True, False):
            env = trust_env if trusted else []
            print(f"{'with' if trusted else 'without'} the platform trust variables")
            out = run("docker", "run", "--rm", "--network", network, *mount, *env, R_IMAGE, "sh", "-c",
                      f"Rscript -e '{R_CLIENTS}' 2>/dev/null; curl -fsS https://tlsserver:8443/ >/dev/null 2>&1 "
                      "&& echo curl-cli=trusted || echo curl-cli=REFUSED", check=False)
            expect("compute-r", out, trusted)
            name = f"{network}-rstudio-{int(trusted)}"
            run("docker", "run", "-d", "--name", name, "--network", network, *mount, *env, "-e", "DISABLE_AUTH=true", RSTUDIO_IMAGE)
            try:
                run("docker", "exec", name, "sh", "-c", "for i in $(seq 60); do [ -f /var/run/s6/container_environment/HOME ] "
                    "&& grep -q TZ /usr/local/lib/R/etc/Renviron.site && exit 0; sleep 1; done; exit 1")
                session = ["docker", "exec", "-u", "rstudio", name, "env", "-i", "HOME=/home/rstudio",
                           "PATH=/usr/local/bin:/usr/bin:/bin"]
                expect("rstudio session", run(*session, "Rscript", "-e", R_CLIENTS, check=False), trusted)
                expect("rstudio session", run(*session, "Rscript", "-e",
                                              f"cat(system({git!r}, intern = TRUE), sep = '\\n')", check=False), trusted)
            finally:
                run("docker", "rm", "-f", name, check=False)
            out = run("docker", "run", "--rm", "--network", network, *mount, *env, "--entrypoint", "sh", VSCODE_IMAGE,
                      "-c", EDITOR_CLIENTS, check=False)
            expect("vscode workspace", out, trusted)
    finally:
        run("docker", "rm", "-f", f"{network}-server", check=False)
        run("docker", "network", "rm", network, check=False)
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        raise SystemExit("execution trust check failed: " + ", ".join(failures))
    print("PASSED: R compute, RStudio sessions and VS Code workspaces trust the private CA only through the mounted platform bundle")


if __name__ == "__main__":
    main()

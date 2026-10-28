"""Egress proxy for workspaces: HTTPS to administrator-allowed host names only.

Run with ``python -m app.egress_proxy``. Workspaces may reach only DNS, the platform and this proxy; it accepts
``CONNECT host:443`` for host names on the allowlist (``github.com``, ``*.githubusercontent.com``), connects
end to end without looking inside TLS, and refuses names that resolve to private, loopback or link-local
addresses, so an allowed name never becomes a way into the cluster. The allowlist is fetched from the control
plane every few seconds; while it cannot be read, the last good one stays in force (and none at start).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import urllib.request
from typing import Iterable, List, Optional

log = logging.getLogger("fdp.egress")
PORT = 443  # HTTPS only: git, package indexes and editor extensions all use it
MAX_HEADER = 16 * 1024


# Public package indexes: workspaces install only from the platform's own package repository, so these are
# refused whatever the allowlist says (CRAN mirrors elsewhere can only be refused by not allowing them).
PACKAGE_INDEXES = ("pypi.org", "*.pypi.org", "pythonhosted.org", "*.pythonhosted.org", "pypi.python.org",
                   "r-project.org", "*.r-project.org", "cran.rstudio.com", "*.posit.co", "*.rstudio.com",
                   "r-universe.dev", "*.r-universe.dev", "bioconductor.org", "*.bioconductor.org",
                   "anaconda.org", "*.anaconda.org", "anaconda.com", "*.anaconda.com", "prefix.dev", "*.prefix.dev")


def package_index(pattern: str) -> bool:
    """Whether a host name or "*." pattern reaches a public package index."""
    pattern = pattern.lower().rstrip(".")
    host = pattern[2:] if pattern.startswith("*.") else pattern
    if allowed(host, PACKAGE_INDEXES):
        return True
    return pattern.startswith("*.") and any(p.lstrip("*.").endswith("." + host) for p in PACKAGE_INDEXES)


def allowed(host: str, patterns: Iterable[str]) -> bool:
    """Exact names, or "*.example.com" for its subdomains (not example.com itself)."""
    host = host.lower().rstrip(".")
    for pattern in patterns:
        pattern = pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def public_addresses(host: str) -> List[str]:
    """The host's addresses, or [] when any of them is not a public internet address."""
    try:
        infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast:
            return []
    return addresses


class Proxy:
    def __init__(self, allowlist_url: str, refresh_seconds: float = 10):
        self.allowlist_url, self.refresh_seconds = allowlist_url, refresh_seconds
        self.patterns: List[str] = []

    def load(self) -> None:
        with urllib.request.urlopen(self.allowlist_url, timeout=5) as response:
            self.patterns = [h["host"] for h in json.load(response).get("hosts", [])]

    async def refresh(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.load)
            except Exception as exc:  # keep the last good allowlist
                log.warning("allowlist unavailable: %s", type(exc).__name__)
            await asyncio.sleep(self.refresh_seconds)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        request = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        target = request[1] if len(request) == 3 and request[0] == "CONNECT" else ""
        host, _, port = target.rpartition(":")
        address = self.check(host, port)
        if isinstance(address, str) and address.startswith("refused:"):
            log.info("refused %s from %s: %s", target or request[:1], peer, address[8:])
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(address, PORT), timeout=15)
        except (OSError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        log.info("connect %s:%s from %s", host, PORT, peer)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(self.pipe(reader, upstream_writer), self.pipe(upstream_reader, writer))

    def check(self, host: str, port: str) -> Optional[str]:
        """The address to connect to, or "refused:<reason>"."""
        if not host or port != str(PORT):
            return "refused:only CONNECT to port 443 is allowed"
        if package_index(host):
            return "refused:packages install from the platform's package repository"
        if not allowed(host, self.patterns):
            return "refused:host is not on the allowlist"
        addresses = public_addresses(host)
        if not addresses:
            return "refused:host does not resolve to public addresses only"
        return addresses[0]

    @staticmethod
    async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            sink.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    proxy = Proxy(os.environ["FDP_EGRESS_ALLOWLIST_URL"])
    asyncio.create_task(proxy.refresh())
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", int(os.environ.get("FDP_EGRESS_PORT", "3128")),
                                        limit=MAX_HEADER)
    log.info("egress proxy listening")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

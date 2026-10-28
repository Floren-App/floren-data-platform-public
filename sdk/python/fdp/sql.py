"""Standard Trino clients with Floren Data Platform authentication and execution-token renewal."""
import os
from urllib.parse import urlsplit


def endpoint(info):
    # Execution-internal and externally configured API addresses may differ.
    base = os.environ.get("FDP_SQL_URL") or os.environ.get("FDP_API") or info["config"]["endpoint"]
    parsed = urlsplit(base)
    if parsed.path.rstrip("/") not in ("", "/api"):
        raise ValueError("FDP_SQL_URL must be an origin URL without a path")
    return parsed


def connection(info, token=None, viewer_token=None, **kwargs):
    from sqlalchemy import URL, create_engine
    import requests
    from . import _auth_token

    parsed = endpoint(info)
    cfg, cred = info["config"], info["credentials"]

    class GatewaySession(requests.Session):
        def request(self, method, url, **options):
            # The client can live longer than its run token. Reuse SDK renewal logic.
            options.setdefault("headers", {})["Authorization"] = "Bearer " + _auth_token(token)
            if viewer_token:
                options["headers"]["X-FDP-Viewer"] = viewer_token
            return super().request(method, url, **options)

    connect_args = {"http_scheme": parsed.scheme, "http_session": GatewaySession(),
                    **kwargs.pop("connect_args", {})}
    url = URL.create("trino", username=cred["username"], host=parsed.hostname,
                     port=parsed.port or (443 if parsed.scheme == "https" else 80),
                     database=cfg["catalog"] + "/" + cfg["schema"])
    return create_engine(url, connect_args=connect_args, **kwargs)

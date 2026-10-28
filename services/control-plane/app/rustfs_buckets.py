"""Sidecar in a layer's RustFS pod: create the configured buckets, then report ready.

Credentials are read from the namespace-local Secret mounted beside the server; they never
pass through the controller. Buckets are only ever created, never deleted.

    python -m app.rustfs_buckets --credentials /rustfs-auth --ready /tmp/buckets-ready lake files
"""
import argparse
import time
from pathlib import Path


def ensure(s3, wanted):
    existing = {bucket["Name"] for bucket in s3.list_buckets().get("Buckets", [])}
    created = [name for name in wanted if name not in existing]
    for name in created:
        s3.create_bucket(Bucket=name)
    return created


def main(argv=None):
    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9000")
    parser.add_argument("--ca", help="CA bundle that verifies an HTTPS endpoint")
    parser.add_argument("buckets", nargs="+")
    args = parser.parse_args(argv)
    secret = Path(args.credentials)
    s3 = boto3.client("s3", endpoint_url=args.endpoint, region_name="us-east-1", verify=args.ca or True,
                      aws_access_key_id=(secret / "access-key").read_text().strip(),
                      aws_secret_access_key=(secret / "secret-key").read_text().strip(),
                      config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1},
                                    connect_timeout=5, read_timeout=10))
    while True:
        try:
            created = ensure(s3, args.buckets)
            break
        except Exception as exc:  # the server is still starting, or the credentials are wrong
            print(f"waiting for RustFS: {type(exc).__name__}", flush=True)
            time.sleep(3)
    print(f"buckets ready; created {', '.join(created) or 'none'}", flush=True)
    Path(args.ready).touch()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

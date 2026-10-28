"""Registers the local example storage as three storage integrations (idempotent).

This is what an administrator does under Administration → Storage integrations, scripted. Every
value describes a location that exists outside Floren Data Platform, in example-storage/docker-compose.yml.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from seed_api import request, wait_ready  # noqa: E402

INTEGRATIONS = [
    # PostgreSQL warehouse "warehouse"; Trino reaches it through trino-catalog/finance.properties.
    {'name': 'finance-dw', 'kind': 'postgres', 'description': 'Finance warehouse (local example storage)',
     'config': {'host': 'warehouse', 'port': 5432, 'database': 'finance', 'catalog': 'finance'},
     # Role used only by the control plane to set up schema access (create roles, grant privileges).
     'provisioner_secret': {'username': 'warehouse_admin', 'password': 'warehouse_admin'}},
    # Iceberg tables in RustFS bucket "lake" via the REST catalog; Trino: trino-catalog/lake.properties.
    {'name': 'lake', 'kind': 'iceberg', 'description': 'Iceberg tables on RustFS (local example storage)',
     'config': {'catalog': 'lake', 'warehouse': 's3://lake/', 'catalog_uri': 'http://iceberg-rest:8181',
                'endpoint_url': 'http://rustfs:9000', 'region': 'us-east-1'}},
    # Plain files in RustFS bucket "files" under prefix "finance"; sessions get scoped STS credentials.
    {'name': 'lake-files', 'kind': 's3', 'description': 'Finance files on RustFS (local example storage)',
     'config': {'bucket': 'files', 'prefix': 'finance', 'region': 'us-east-1', 'endpoint_url': 'http://rustfs:9000',
                'sts_endpoint_url': 'http://rustfs:9000', 'role_arn': 'arn:aws:iam::123456789012:role/fdp'},
     'provisioner_secret': {'access_key_id': 'fdp', 'secret_access_key': 'fdp-rustfs-secret'}},
]

wait_ready()
for integration in INTEGRATIONS:
    request('PUT', f"/v1/admin/datasources/{integration['name']}", integration)
    print(f"  {integration['name']:<11} {integration['kind']:<8} {integration['description']}")
print('Registered the example storage. Open Administration → Storage integrations to review it.')

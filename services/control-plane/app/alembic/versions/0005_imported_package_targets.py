"""Give imported packages individual permission targets without granting access."""
import hashlib
from alembic import op
import sqlalchemy as sa

revision = '0005_imported_package_targets'
down_revision = '0004_kafka_session_group'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    packages = sa.table('packages', sa.column('id', sa.String(12)),
        sa.column('name', sa.String(200)), sa.column('kind', sa.String(8)),
        sa.column('resource_id', sa.String(12)))
    resources = sa.table('rbac_resources', sa.column('id', sa.String(12)),
        sa.column('kind', sa.String(16)), sa.column('name', sa.String(256)),
        sa.column('parent_id', sa.String(12)), sa.column('object_id', sa.String(12)),
        sa.column('config', sa.JSON()))
    missing = connection.execute(sa.select(packages).where(packages.c.resource_id.is_(None))).mappings().all()
    if missing and not connection.scalar(sa.select(resources.c.id).where(resources.c.id == 'packages')):
        connection.execute(resources.insert().values(id='packages', kind='package-root', name='Packages', config={}))
    for package in missing:
        ident = hashlib.sha256(('fdp-package:'+package['id']).encode()).hexdigest()[:12]
        if connection.scalar(sa.select(resources.c.id).where(resources.c.id == ident)):
            raise RuntimeError('package permission target collision')
        connection.execute(resources.insert().values(id=ident, kind='package', name=package['name'],
            parent_id='packages', object_id=package['id'], config={'package_kind':package['kind']}))
        connection.execute(packages.update().where(packages.c.id == package['id']).values(resource_id=ident))


def downgrade():
    raise RuntimeError('Do not remove package permission targets; use recovery')

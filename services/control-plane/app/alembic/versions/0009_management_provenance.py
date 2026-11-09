"""Management provenance: which objects Terraform manages, and app changes since it last applied them."""
from alembic import op
import sqlalchemy as sa
from app.schema_migrations import baseline_table

revision = '0009_management_provenance'
down_revision = '0008_several_roles_per_object'
branch_labels = None
depends_on = None


def upgrade():
    baseline_table(
        'management_provenance',
        sa.Column('target', sa.String(128), primary_key=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('reference', sa.String(512), nullable=False, server_default=''),
        sa.Column('revision', sa.String(128), nullable=False, server_default=''),
        sa.Column('managed_by', sa.String(128), nullable=False),
        sa.Column('managed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('app_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('app_changed_by', sa.String(128), nullable=True),
        sa.Column('app_change', sa.String(64), nullable=True),
    )


def downgrade():
    op.drop_table('management_provenance')

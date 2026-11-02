"""Freeze masks and row filters alongside run data authority."""
from alembic import op, context
import sqlalchemy as sa

revision = '0006_run_privacy_snapshot'
down_revision = '0005_imported_package_targets'
branch_labels = None
depends_on = None


def upgrade():
    if context.config.attributes.get('adopt_legacy') and 'sql_policy_snapshot' in {
        c['name'] for c in sa.inspect(op.get_bind()).get_columns('runs')}:
        return
    # Existing admissions cannot safely reconstruct their former privacy policy.
    # Their new SQL admissions fail closed; finish/drain jobs before upgrading.
    op.add_column('runs',sa.Column('sql_policy_snapshot',sa.JSON(),nullable=True))


def downgrade():
    raise RuntimeError('Do not remove privacy admission snapshots; use recovery')

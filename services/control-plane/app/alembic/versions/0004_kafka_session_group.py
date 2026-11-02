"""Bind broker decisions to the group captured at Kafka admission."""
from alembic import op, context
import sqlalchemy as sa

revision = '0004_kafka_session_group'
down_revision = '0003_named_data_snapshot'
branch_labels = None
depends_on = None


def upgrade():
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('kafka_sessions')}
    if not (context.config.attributes.get('adopt_legacy') and 'active_group_id' in existing):
        op.add_column('kafka_sessions', sa.Column('active_group_id', sa.String(12), nullable=False,
                                                server_default=sa.text("''")))
    # Older sessions did not record their selected group and cannot be reconstructed safely.
    op.execute(sa.text('UPDATE kafka_sessions SET expires_at = 0'))


def downgrade():
    raise RuntimeError('Do not remove Kafka admission boundaries; use recovery')

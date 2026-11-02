"""Per-object named data authority fixed at run admission."""
from alembic import op, context
import sqlalchemy as sa
revision = "0003_named_data_snapshot"
down_revision = "0002_platform_backups"
branch_labels = None
depends_on = None

def upgrade():
    if context.config.attributes.get("adopt_legacy") and "named_access_snapshot" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}:
        return
    op.add_column("runs", sa.Column("named_access_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

def downgrade():
    raise RuntimeError("Do not remove authorization snapshots; use the recovery procedure")

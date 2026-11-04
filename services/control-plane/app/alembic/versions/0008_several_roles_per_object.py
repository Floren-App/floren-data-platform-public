"""A group or identity can hold several roles on one object; role names get room for the explicit role families."""
from alembic import op
import sqlalchemy as sa

revision = '0008_several_roles_per_object'
down_revision = '0007_private_credential_identity'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table, recipient in (('role_assignments', 'app_group_id'), ('service_role_assignments', 'principal_id')):
        constraints = inspector.get_unique_constraints(table)
        name = f'uq_{table}_recipient_role'
        # SQLite reports the original constraint without a name; batch mode names it by convention.
        convention = {'uq': 'uq_%(table_name)s_%(column_0_N_name)s'}
        with op.batch_alter_table(table, naming_convention=convention) as batch:
            batch.alter_column('role', type_=sa.String(32), existing_type=sa.String(16), existing_nullable=False)
            for old in constraints:
                if set(old['column_names']) == {'resource_id', recipient}:
                    batch.drop_constraint(old['name'] or f'uq_{table}_resource_id_{recipient}', type_='unique')
            if name not in {c['name'] for c in constraints}:
                batch.create_unique_constraint(name, ['resource_id', recipient, 'role'])


def downgrade():
    raise RuntimeError('Groups may now hold several roles per object; use recovery to go back')

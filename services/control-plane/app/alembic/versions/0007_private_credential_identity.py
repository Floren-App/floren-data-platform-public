"""Bind private credentials to immutable directory identities rather than reusable names."""
from alembic import op, context
import sqlalchemy as sa

revision='0007_private_credential_identity'
down_revision='0006_run_privacy_snapshot'
branch_labels=None
depends_on=None


def upgrade():
    connection=op.get_bind()
    for table,column in [('git_credentials',sa.Column('principal_id',sa.String(12),nullable=True)),
        ('git_credentials',sa.Column('legacy_token_allowed',sa.Boolean(),nullable=False,server_default=sa.false())),
        ('secret_entries',sa.Column('owner_principal_id',sa.String(12),nullable=True))]:
        names={c['name'] for c in sa.inspect(connection).get_columns(table)}
        if not (context.config.attributes.get('adopt_legacy') and column.name in names):
            op.add_column(table,column)
    principals={}
    for row in connection.execute(sa.text('SELECT id,kind,name FROM directory_principals')):
        principals.setdefault((row.kind,row.name),[]).append(row.id)
    for table,column,private in [('git_credentials','principal_id',False),('secret_entries','owner_principal_id',True)]:
        columns='id,owner,scope' if private else 'id,owner'
        for row in connection.execute(sa.text('SELECT '+columns+' FROM '+table)):
            if private and row.scope=='group':
                continue
            kind='service' if private and row.scope=='sa' else 'user'
            candidates=principals.get((kind,row.owner),[])
            owner=candidates[0] if len(candidates)==1 else 'unbound'
            connection.execute(sa.text('UPDATE '+table+' SET '+column+'=:owner WHERE id=:id AND '+column+' IS NULL'),{'owner':owner,'id':row.id})
            if not private and owner!='unbound':
                connection.execute(sa.text('UPDATE git_credentials SET legacy_token_allowed=:enabled WHERE id=:id'),{'enabled':True,'id':row.id})

    for table in ('compliance_cases','catalog_access_requests'):
        for row in connection.execute(sa.text('SELECT id,requester,requester_principal_id FROM '+table)):
            if row.requester_principal_id:
                continue
            candidates=principals.get(('user',row.requester),[])
            owner=candidates[0] if len(candidates)==1 else 'unbound'
            connection.execute(sa.text('UPDATE '+table+' SET requester_principal_id=:owner WHERE id=:id'),{'owner':owner,'id':row.id})


def downgrade():
    raise RuntimeError('Do not discard private credential identity bindings; use recovery')

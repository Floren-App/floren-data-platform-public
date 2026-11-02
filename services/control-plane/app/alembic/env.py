from alembic import context

connection = context.config.attributes.get('connection')
if connection is None:
    raise RuntimeError('Use python -m app.schema_migrations; migration connections must be explicitly supplied')
context.configure(connection=connection, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()

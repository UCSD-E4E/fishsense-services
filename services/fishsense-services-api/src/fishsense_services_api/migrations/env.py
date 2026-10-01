"""Alembic environment: synchronous psycopg over the configured URL."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

url = make_url(context.config.get_main_option("sqlalchemy.url"))
engine = create_engine(url.set(drivername="postgresql+psycopg"))

with engine.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()

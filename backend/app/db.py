from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        engine = create_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    connect_args = {}
    if database_url.startswith("postgresql+psycopg"):
        # Neon's pooled endpoint runs PgBouncer in transaction mode, which doesn't
        # get along with server-side prepared statements.
        connect_args["prepare_threshold"] = None
    return create_engine(
        database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,  # Neon suspends idle computes; re-check connections
        pool_recycle=300,
        connect_args=connect_args,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()

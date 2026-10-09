from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    if not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("DATABASE_URL must be a PostgreSQL URL starting with postgresql+psycopg://")
    return create_engine(
        database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,  # Neon suspends idle computes; re-check connections
        pool_recycle=300,
        # Neon's pooled endpoint runs PgBouncer in transaction mode, which doesn't
        # get along with server-side prepared statements.
        connect_args={"prepare_threshold": None},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)

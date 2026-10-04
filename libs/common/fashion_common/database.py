
import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Add it to your .env file.")
    return url


def make_engine(url: str | None = None) -> Engine:
    # pool_pre_ping checks a pooled connection is still alive before using it.
    return create_engine(url or get_database_url(), pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps objects readable after commit.
    return sessionmaker(bind=engine, expire_on_commit=False)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for PostgreSQL persistence models only."""

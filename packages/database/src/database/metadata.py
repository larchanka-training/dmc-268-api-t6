"""Assembly of shared SQLAlchemy metadata for the migration tool."""

from database.base import Base
from database.models import (
    analytics,  # noqa: F401
    billing,  # noqa: F401
    repositories,  # noqa: F401
    reviews,  # noqa: F401
    webhooks,  # noqa: F401
    workspaces,  # noqa: F401
)

metadata = Base.metadata

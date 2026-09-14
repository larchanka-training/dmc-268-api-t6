"""Composition root: import module models to assemble Alembic target metadata."""

from app.common.infrastructure.db.base import Base
from app.modules.analytics.infrastructure import models as analytics_models  # noqa: F401
from app.modules.billing.infrastructure import models as billing_models  # noqa: F401
from app.modules.integrations.webhooks.infrastructure import (
    models as integrations_webhooks_models,  # noqa: F401
)
from app.modules.repositories.infrastructure import models as repositories_models  # noqa: F401
from app.modules.reviews.infrastructure import models as reviews_models  # noqa: F401
from app.modules.workspaces.infrastructure import models as workspaces_models  # noqa: F401

metadata = Base.metadata

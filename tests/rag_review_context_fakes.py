"""The caller source the graph RAG context tests index: ``pkg/app.py`` and its edges."""

from __future__ import annotations

from app.modules.code_index.application.index_store import ImportRecord, IndexedFile
from app.modules.reviews.application.ast_resolution import ImportResolution
from tests.code_index_postgres import _imports, _uses

# `pkg/app.py` calls the `greet` the PR changes; the default-branch graph knows it.
_USER = "from .service import greet\n\n\ndef welcome(user_id):\n    return greet(user_id)\n"
# The same caller with a trailing comment, for the comment gate (#498).
_USER_NOTED = _USER.replace("greet(user_id)\n", "greet(user_id)  # note\n")


def _user_file() -> IndexedFile:
    return IndexedFile(
        "pkg/app.py",
        "a1" * 20,
        edges=(
            _imports("pkg/app.py", "pkg/service.py"),
            _uses("pkg/app.py", "pkg/service.py", "greet"),
        ),
        imports=(
            ImportRecord("pkg/app.py", ".service", ImportResolution.RESOLVED, "pkg/service.py"),
        ),
    )


_SERVICE = '''from .models import User
from .secret import token


def unrelated():
    return 0


def greet(user_id):
    """Greet a user."""
    user = User(user_id)
    extra = 1
    return "hi " + user.name
'''


_MODELS = '''class User:
    """A user of the system."""

    def __init__(self, user_id):
        self.name = str(user_id)
'''


_GREET_PATCH = (
    "@@ -9,4 +9,5 @@\n def greet(user_id):\n"
    '     """Greet a user."""\n     user = User(user_id)\n+    extra = 1\n'
    '     return "hi " + user.name'
)

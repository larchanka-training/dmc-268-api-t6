"""Which review path a run takes: chosen by the review prompt the run pinned (#130 F1).

A run bound to a prompt of the snapshot contract is reviewed with its policy snapshot: the
snapshot envelope in, ReviewOutput v2 out, the verifier after. Every other run keeps the
legacy path, also when it has a ready snapshot (a run in flight, a run created before the
pins existed). A snapshot-path run that cannot be served fails with
``policy_snapshot_integrity_error`` before any model call; it never falls back to legacy,
which would validate the snapshot prompt's answer against the v1 schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.modules.reviews.application.diff_map import VcsProviderName
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot

# Review prompt versions written for the snapshot envelope and ReviewOutput v2; the one
# place a new snapshot-contract version (#144: v5) is added.
SNAPSHOT_REVIEW_PROMPT_VERSIONS: frozenset[str] = frozenset({"v4"})
# TL-13: the context stage builds the diff map as GitHub, so a run of another provider stays
# on the legacy path until the stage takes the provider from the run (#220 extends this).
SNAPSHOT_REVIEW_PROVIDERS: frozenset[str] = frozenset({VcsProviderName.GITHUB.value})


@dataclass(frozen=True)
class ReviewBinding:
    """The review prompt a run pinned (``Run.prompt_version_id``) and where the run came from.

    ``review_system_version`` is ``v{PromptVersion.version}`` of the pinned row, as in the
    policy pins. ``prompt_version_id`` is ``None`` only for prompt bindings built without the
    run's pins (tests); a run read always names the row, and such a binding is never served.
    ``provider`` is the provider of the run's origin installation, ``None`` for a run without
    one; it is compared as stored, never coerced.
    """

    prompt_version_id: UUID | None
    review_system_version: str
    provider: str | None

    @property
    def takes_snapshot_path(self) -> bool:
        return (
            self.review_system_version in SNAPSHOT_REVIEW_PROMPT_VERSIONS
            and self.provider in SNAPSHOT_REVIEW_PROVIDERS
        )


def snapshot_for_review(
    binding: ReviewBinding, snapshot: PolicySnapshot | None
) -> PolicySnapshot | None:
    """The policy snapshot the review of this run is given; ``None`` on the legacy path.

    A snapshot-path run needs the attempt's snapshot, ready for the LLM and pinning the
    same review prompt as the run; anything else is refused, before any model call.
    """
    if not binding.takes_snapshot_path:
        return None
    if snapshot is None:
        raise _refused(binding, "no policy snapshot was established for the attempt")
    if not snapshot.is_ready_for_llm:
        raise _refused(binding, "the policy snapshot is not ready for the LLM")
    if binding.prompt_version_id is None or not snapshot.prompt_bindings.pins_review_prompt(
        binding.prompt_version_id
    ):
        raise _refused(binding, "the policy snapshot pins another review prompt")
    return snapshot


def _refused(binding: ReviewBinding, reason: str) -> PolicyError:
    return PolicyError(
        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
        f"Review prompt {binding.review_system_version} takes the policy snapshot path, "
        f"but {reason}",
    )

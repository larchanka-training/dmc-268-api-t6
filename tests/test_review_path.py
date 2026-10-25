"""The review path is chosen by the review prompt a run pinned (#130 F1-2).

A run bound to a snapshot-contract ``review.system`` prompt (v4) takes the policy-snapshot path;
any other run keeps the legacy path, also with a ready snapshot. A snapshot-path run that cannot
be served (no usable snapshot, or a provider the context stage does not support yet, TL-13)
fails with ``policy_snapshot_integrity_error`` before the conventions call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    PolicySnapshot,
    PromptBindings,
    make_complete_execution_profile,
)
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import (
    ReviewContextStagePort,
    ReviewRunProcessor,
    RunConventionsInput,
)
from app.modules.reviews.application.prompt_budget import policy_tokens, token_counter_for
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_path import (
    SNAPSHOT_REVIEW_PROMPT_VERSIONS,
    ReviewBinding,
    snapshot_for_review,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.llm import answers
from tests.test_prepare_policy_snapshot import Source, Store, Uow, _pins
from tests.test_review_context_stage import (
    Parser,
    Runs,
    Trace,
    Vcs,
    _checkpoint,
    _snapshots,
    _stored,
)
from tests.test_review_context_stage import Repository as SourceTree
from tests.test_review_context_stage import _stage as _context_stage
from tests.test_run_processor_context_stage import (
    REPOSITORY_ID,
    RUN_ID,
    Conventions,
    GitHub,
    Stage,
)
from tests.test_run_processor_context_stage import Repository as ProcessingRepository
from tests.trigger_uow import processing_uow

_REVIEW_V4 = UUID(int=41)
_OTHER_REVIEW = UUID(int=42)
_CONVENTIONS = UUID(int=43)
_VERIFIER = UUID(int=44)
_INTEGRITY = PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR


def _binding(
    version: str = "v4", provider: str | None = "github", *, key: str = "review.system"
) -> ReviewBinding:
    return ReviewBinding(_REVIEW_V4, key, version, provider)


def _store(
    version: str = "v4",
    *,
    review_id: UUID = _REVIEW_V4,
    conventions: UUID | None = _CONVENTIONS,
    max_policy_tokens: int = 12_000,
    base: str | None = None,
    counter_version: str = "counter/v2",
) -> Store:
    """Pins as get_pins reads them; a NULL conventions pin leaves the bindings incomplete."""
    bindings = PromptBindings(
        review_system_checksum="3" * 64,
        conventions_checksum="" if conventions is None else "4" * 64,
        verifier_checksum="5" * 64,
        review_system_version=version,
        verifier_version="v2",
        review_system_id=review_id,
        conventions_id=conventions,
        verifier_id=_VERIFIER,
    )
    profile = replace(
        make_complete_execution_profile(),
        max_policy_tokens=max_policy_tokens,
        counter_version=counter_version,
    )
    pins = _pins(prompt_bindings=bindings, execution_profile=profile)
    return Store(pins=pins if base is None else replace(pins, policy_base_sha=base))


def _snapshot(store: Store) -> PolicySnapshot:
    prepare = PreparePolicySnapshot(
        uow_factory=lambda: Uow(store), source_factory=lambda p: Source()
    )
    return asyncio.run(prepare.execute(RUN_ID))


def test_the_snapshot_contract_has_one_source() -> None:
    assert frozenset({"v4"}) == SNAPSHOT_REVIEW_PROMPT_VERSIONS
    assert answers.review_answer_contract("v4").name == "ReviewOutputV2"
    assert answers.review_answer_contract("v3").name == "ReviewOutput"
    assert not hasattr(answers, "_V2_REVIEW_PROMPT_VERSIONS")


@pytest.mark.parametrize(
    ("key", "version", "provider", "snapshot_path"),
    [
        ("review.system", "v3", "github", False),
        ("review.system", "v2", "github", False),
        ("review.system", "v4", "github", True),
        # A legacy version is never refused, whatever the provider.
        ("review.system", "v3", "gitlab", False),
        ("review.system", "v3", None, False),
        # Only a review.system row picks the path: another key's v4 is not a review prompt.
        ("review.conventions", "v4", "github", False),
        ("review.verifier", "v4", "github", False),
    ],
    ids=[
        "v3",
        "v2",
        "v4-github",
        "v3-gitlab",
        "v3-no-origin",
        "conventions-key-v4",
        "verifier-key-v4",
    ],
)
def test_the_pinned_review_prompt_chooses_the_path(
    key: str, version: str, provider: str | None, snapshot_path: bool
) -> None:
    binding = _binding(version, provider, key=key)
    # A ready snapshot that pins the bound prompt: only the binding decides.
    snapshot = _snapshot(_store(version))

    assert binding.takes_snapshot_path is snapshot_path
    assert snapshot_for_review(binding, snapshot) is (snapshot if snapshot_path else None)


@pytest.mark.parametrize(
    ("provider", "named"),
    [
        # TL-13: the stage builds a GitHub diff map; a v4 run of another provider must not
        # fall to legacy, which would validate the v4 answer with the v1 contract.
        ("gitlab", "'gitlab'"),
        # No coercion: only the exact provider name is served.
        ("GitHub", "'GitHub'"),
        ("", "''"),
        (None, "no origin provider"),
    ],
    ids=["gitlab", "unknown-provider", "empty-provider", "no-origin"],
)
def test_a_snapshot_path_run_on_an_unsupported_provider_is_refused(
    provider: str | None, named: str
) -> None:
    # Even a ready snapshot that pins the bound prompt does not serve it.
    snapshot = _snapshot(_store())

    with pytest.raises(PolicyError) as raised:
        snapshot_for_review(_binding("v4", provider), snapshot)

    assert raised.value.code is _INTEGRITY
    assert str(raised.value).startswith(
        "policy_snapshot_integrity_error: Review prompt v4 takes the policy snapshot path, "
        "but the context stage does not serve "
    )
    assert named in str(raised.value)


@pytest.mark.parametrize(
    ("store", "message"),
    [
        (None, "no policy snapshot"),
        # D-Q2/Q4: a NULL conventions pin leaves the snapshot not ready.
        (_store(conventions=None), "not ready for the LLM"),
        (_store(review_id=_OTHER_REVIEW), "pins another review prompt"),
    ],
    ids=["no-snapshot", "null-pin", "pins-mismatch"],
)
def test_a_snapshot_path_run_that_cannot_be_served_is_refused(
    store: Store | None, message: str
) -> None:
    snapshot = None if store is None else _snapshot(store)

    with pytest.raises(PolicyError) as raised:
        snapshot_for_review(_binding(), snapshot)

    assert raised.value.code is _INTEGRITY
    assert message in str(raised.value)
    assert "v4" in str(raised.value)
    # The same inputs on the legacy path are never refused.
    assert snapshot_for_review(_binding("v3"), snapshot) is None


def _stage(store: Store, trace: Trace) -> ReviewContextStage:
    vcs = Vcs()
    return ReviewContextStage(
        runs=Runs(None),
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Source()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=SourceTree(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
    )


# The stage reads the binding off its snapshot, whose prompt bindings are the run's pins
# (checked on every read of a stored snapshot), and the provider it builds the diff map with
# (GitHub until #220). So the GitLab and pins-mismatch cases are pinned at the selector, the
# processor without a stage, execute_review and the database reads, not here.
@pytest.mark.parametrize(
    ("version", "gets_snapshot"),
    [("v2", False), ("v3", False), ("v4", True)],
    ids=["v2", "v3", "v4"],
)
def test_stage_gives_the_snapshot_only_to_a_snapshot_path_run(
    version: str, gets_snapshot: bool
) -> None:
    store, trace = _store(version), Trace()
    stage = _stage(store, trace)

    asyncio.run(stage.begin(RUN_ID))

    assert store.snapshot is not None and store.snapshot.is_ready_for_llm
    assert stage.snapshot_for_llm is (store.snapshot if gets_snapshot else None)
    assert [tool for tool, _, _ in trace.steps] == ["policy.load"]
    assert "error" not in trace.steps[0][2]


def test_stage_refuses_a_snapshot_path_run_at_policy_load() -> None:
    # D-Q2/Q4: a v4 run with a NULL conventions pin.
    trace = Trace()
    stage = _stage(_store(conventions=None), trace)

    with pytest.raises(PolicyError) as raised:
        asyncio.run(stage.begin(RUN_ID))

    assert raised.value.code is _INTEGRITY
    assert stage.snapshot_for_llm is None
    [(tool, _, response)] = trace.steps
    assert tool == "policy.load"
    assert response["error"]["type"] == "PolicyError"
    assert response["error"]["message"] == (
        "policy_snapshot_integrity_error: Review prompt v4 takes the policy snapshot path, "
        "but the policy snapshot is not ready for the LLM"
    )


@dataclass
class BoundRepository(ProcessingRepository):
    binding: ReviewBinding | None = None

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(
            REPOSITORY_ID,
            ActiveConventionsPrompt(RUN_ID, "prompt"),
            review_binding=self.binding,
        )


@dataclass
class CountingGitHub(GitHub):
    reads: list[str] = field(default_factory=list)

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        self.reads.append("pull request")
        return await super().get_pull_request(locator, checkpoint=checkpoint)


def _processor(
    binding: ReviewBinding | None,
    events: list[str],
    github: CountingGitHub,
    *,
    stage: ReviewContextStagePort | None = None,
) -> ReviewRunProcessor:
    repository = BoundRepository(events, binding=binding)
    options = {} if stage is None else {"context_stage": stage}
    return ReviewRunProcessor(
        repository,
        None,
        blob_cache=InMemoryBlobCache(),
        conventions=cast(GenerateRepoConventions, Conventions(events)),
        vcs_provider=github,
        uow_factory=processing_uow(repository),
        **options,  # type: ignore[arg-type]
    )


def test_processor_with_the_stage_refuses_before_the_diff_and_the_conventions_call() -> None:
    events: list[str] = []
    github = CountingGitHub()
    stage = _stage(_store(conventions=None), Trace())

    with pytest.raises(PolicyError) as raised:
        asyncio.run(_processor(_binding(), events, github, stage=stage).prepare(RUN_ID))

    assert raised.value.code is _INTEGRITY
    assert events == []
    assert github.reads == []


@pytest.mark.parametrize(
    ("binding", "refused"),
    [
        (_binding("v4"), "no policy snapshot"),
        (_binding("v4", "gitlab"), "does not serve the provider 'gitlab'"),
        (_binding("v4", None), "no origin provider"),
        (_binding("v3"), None),
        (_binding("v3", "gitlab"), None),
        (None, None),
    ],
    ids=["v4-github", "v4-gitlab", "v4-no-origin", "v3", "v3-gitlab", "no-binding"],
)
def test_processor_without_the_stage_refuses_a_snapshot_path_run_before_the_diff(
    binding: ReviewBinding | None, refused: str | None
) -> None:
    events: list[str] = []
    github = CountingGitHub()
    processor = _processor(binding, events, github)

    if refused is not None:
        with pytest.raises(PolicyError) as raised:
            asyncio.run(processor.prepare(RUN_ID))
        assert raised.value.code is _INTEGRITY
        assert refused in str(raised.value)
        assert events == []
        assert github.reads == []
    else:
        assert isinstance(asyncio.run(processor.prepare(RUN_ID)), GeneratedConventions)
        assert events == ["diff stored", "conventions model"]


@pytest.mark.parametrize(
    ("binding", "refused"),
    [
        (_binding("v4", "gitlab"), "does not serve the provider 'gitlab'"),
        (_binding("v4", None), "no origin provider"),
        (_binding("v4"), None),
        (_binding("v3", "gitlab"), None),
    ],
    ids=["v4-gitlab", "v4-no-origin", "v4-github", "v3-gitlab"],
)
def test_processor_with_a_stage_refuses_an_unserved_provider_before_the_policy_load(
    binding: ReviewBinding, refused: str | None
) -> None:
    # The stage builds its diff map as GitHub, so its own check at policy load passes a run of
    # any provider (the stub passes everything); the processor checks the run's provider
    # before the stage loads the policy, so a policy error cannot mask the refusal.
    events: list[str] = []
    github = CountingGitHub()
    processor = _processor(binding, events, github, stage=Stage(events))

    if refused is not None:
        with pytest.raises(PolicyError) as raised:
            asyncio.run(processor.prepare(RUN_ID))
        assert raised.value.code is _INTEGRITY
        assert refused in str(raised.value)
        assert events == []
        assert github.reads == []
    else:
        assert isinstance(asyncio.run(processor.prepare(RUN_ID)), GeneratedConventions)
        assert events == ["policy loaded", "diff stored", "policy stage", "conventions model"]


@dataclass
class NoConventionsPromptRepository(BoundRepository):
    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        raise LookupError("active review.conventions prompt is missing")


@pytest.mark.parametrize("with_stage", [True, False], ids=["stage", "no-stage"])
def test_a_missing_conventions_prompt_fails_before_the_policy_load_and_the_diff(
    with_stage: bool,
) -> None:
    # The conventions input is read first: a missing active prompt ends the attempt with
    # internal_error before policy.load, any diff read and any diff write.
    events: list[str] = []
    github = CountingGitHub()
    repository = NoConventionsPromptRepository(events, binding=_binding("v3"))
    options = {"context_stage": Stage(events)} if with_stage else {}
    processor = ReviewRunProcessor(
        repository,
        None,
        blob_cache=InMemoryBlobCache(),
        conventions=cast(GenerateRepoConventions, Conventions(events)),
        vcs_provider=github,
        uow_factory=processing_uow(repository),
        **options,  # type: ignore[arg-type]
    )

    with pytest.raises(LookupError, match="active review.conventions prompt is missing"):
        asyncio.run(processor.prepare(RUN_ID))

    assert events == []
    assert github.reads == []


# ---------- #130 F1-10: conventions get the pinned prompt and the snapshot (R1, T-c) ----------

_ACTIVE = ActiveConventionsPrompt(UUID(int=51), "active conventions")
_PINNED = ActiveConventionsPrompt(_CONVENTIONS, "pinned conventions")


@dataclass
class PinnedRepository(BoundRepository):
    pinned: ActiveConventionsPrompt | None = _PINNED

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(
            REPOSITORY_ID,
            _ACTIVE,
            review_binding=self.binding,
            pinned_conventions_prompt=self.pinned,
        )


@dataclass
class RecordingConventions:
    events: list[str]
    calls: list[dict[str, object]] = field(default_factory=list)

    async def execute(self, **kwargs: object) -> GeneratedConventions:
        self.events.append("conventions model")
        self.calls.append(kwargs)
        return GeneratedConventions(
            CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {}), None, False
        )


def _pinned_prepare(
    binding: ReviewBinding,
    snapshot: PolicySnapshot | None,
    pinned: ActiveConventionsPrompt | None = _PINNED,
    *,
    events: list[str] | None = None,
    github: CountingGitHub | None = None,
) -> tuple[RecordingConventions, list[str]]:
    events = [] if events is None else events
    repository = PinnedRepository(events, binding=binding, pinned=pinned)
    conventions = RecordingConventions(events)
    asyncio.run(
        ReviewRunProcessor(
            repository,
            None,
            blob_cache=InMemoryBlobCache(),
            conventions=cast(GenerateRepoConventions, conventions),
            vcs_provider=CountingGitHub() if github is None else github,
            uow_factory=processing_uow(repository),
            context_stage=Stage(events, snapshot_for_llm=snapshot),
        ).prepare(RUN_ID)
    )
    return conventions, events


def test_snapshot_path_conventions_get_the_pinned_prompt_and_the_snapshot() -> None:
    snapshot = _snapshot(_store())

    conventions, _ = _pinned_prepare(_binding("v4"), snapshot)

    [call] = conventions.calls
    assert call["conventions_prompt"] == _PINNED
    assert call["snapshot"] is snapshot


def test_legacy_conventions_keep_the_active_prompt_and_no_snapshot() -> None:
    conventions, _ = _pinned_prepare(_binding("v3"), None)

    [call] = conventions.calls
    assert call["conventions_prompt"] == _ACTIVE
    assert "snapshot" not in call


@pytest.mark.parametrize(
    "pinned",
    [None, ActiveConventionsPrompt(UUID(int=52), "another pinned row")],
    ids=["no-pin", "another-row"],
)
def test_snapshot_path_conventions_refuse_a_pin_the_snapshot_does_not_name(
    pinned: ActiveConventionsPrompt | None,
) -> None:
    events: list[str] = []
    github = CountingGitHub()

    with pytest.raises(PolicyError) as raised:
        _pinned_prepare(_binding("v4"), _snapshot(_store()), pinned, events=events, github=github)

    assert raised.value.code is _INTEGRITY
    assert "conventions prompt" in str(raised.value)
    # Checked right after the policy load: no diff read or write and no stage run.
    assert events == ["policy loaded"]
    assert github.reads == []


# ---------- #130 F1-11: snapshot-path conventions keyed by repo-conventions-v2 (R5) ----------


# The repo-conventions-v2 key of the fixture policy (``Source``), its ignore digest untainted.
_V2_DIGEST = "fef4efc6dfdb3c7e9110ffd7b6f0f33259cd6bf2a2116efa68aa0e71ac45f898"


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_stage_policy_key_is_the_v2_conventions_key_only_on_the_snapshot_path(
    version: str,
) -> None:
    store = _store(version)
    stage = _stage(store, Trace())

    asyncio.run(stage.begin(RUN_ID))

    assert store.snapshot is not None
    ignore_key = store.snapshot.ignore_digest
    assert stage.policy_key == (_V2_DIGEST if version == "v4" else ignore_key)
    assert stage.policy_key != (ignore_key if version == "v4" else None)


def test_stage_v2_key_is_the_same_for_another_pr_base_with_the_same_policy() -> None:
    # Tech lead, 2026-10-25 (TL-7): the PR base is not part of the key, so the next pull
    # request of the repository reuses the entry while the policy stays the same.
    observed: list[tuple[str, str | None]] = []
    for base in ("a" * 40, "e" * 40):
        store = _store(base=base)
        stage = _stage(store, Trace())
        asyncio.run(stage.begin(RUN_ID))
        assert store.snapshot is not None and store.snapshot.policy_base_sha == base
        observed.append((store.snapshot.policy_digest, stage.policy_key))

    [(first_digest, first_key), (second_digest, second_key)] = observed
    assert first_digest != second_digest
    assert first_key == second_key == _V2_DIGEST


# The repo-conventions-v2 keys of the fixture policy without and with the denied rename.
_PLAIN_DIGEST = "cf2ce488d691d5229dc0cf4464d1f4cac55ab4baf125c9342928237607609dc4"
_TAINTED_DIGEST = "754d1058a2ba36df1f6da47237da247d4a70ad7915132a76eff1cc299fd815f8"


def test_stage_v2_key_carries_the_taint_of_a_denied_rename() -> None:
    # The key is built over the stage's ignore key: a rename out of an ignored path withholds
    # a name no mask matches, so an entry made while it was allowed is not reused.
    moved = DiffSnapshot(
        "src/moved.py",
        _stored("src/moved.py", "@@ -1 +1 @@\n-a\n+b"),
        "f" * 40,
        "renamed",
        "generated/moved.py",
        1,
        1,
        2,
    )
    plain = _context_stage(Trace(), store=_store())
    tainted = _context_stage(Trace(), store=_store(), snapshots=[*_snapshots(), moved])

    asyncio.run(plain.run(RUN_ID))
    asyncio.run(tainted.run(RUN_ID))

    assert plain.snapshot_for_llm is not None and tainted.snapshot_for_llm is not None
    assert tainted.excluded("src/moved.py") is True
    assert plain.policy_key == _PLAIN_DIGEST
    assert tainted.policy_key == _TAINTED_DIGEST


# ---------- #130 F1-12: the policy budget is checked at policy load (R7, TL-8) ----------

# The rules.md and AGENTS.md of the fixture source, rendered and counted with counter/v2.
_POLICY_TOKENS = 105


def test_the_fixture_policy_has_the_counted_size() -> None:
    snapshot = _snapshot(_store())
    counter = token_counter_for(snapshot.execution_profile.counter_version)

    assert policy_tokens(snapshot, counter) == _POLICY_TOKENS


@pytest.mark.parametrize(
    ("limit", "refused"),
    [(_POLICY_TOKENS - 1, True), (_POLICY_TOKENS, False)],
    ids=["over", "at-limit"],
)
def test_stage_refuses_a_v4_run_over_the_policy_budget_at_policy_load(
    limit: int, refused: bool
) -> None:
    store, trace = _store(max_policy_tokens=limit), Trace()
    stage = _stage(store, trace)

    if not refused:
        asyncio.run(stage.begin(RUN_ID))
        assert stage.snapshot_for_llm is store.snapshot
        return
    with pytest.raises(PolicyError) as raised:
        asyncio.run(stage.begin(RUN_ID))

    assert raised.value.code is PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert (raised.value.limit, raised.value.actual) == (limit, _POLICY_TOKENS)
    assert stage.snapshot_for_llm is None
    [(tool, _, response)] = trace.steps
    assert tool == "policy.load"
    assert response["error"]["message"] == (
        "policy_prompt_budget_exceeded: The rendered policy documents exceed the token budget "
        f"of the pinned profile | фактически {_POLICY_TOKENS}, допустимо {limit}"
    )


def test_stage_counts_the_policy_with_the_counter_of_the_pinned_profile() -> None:
    # R7: counter/v1 counts the fixture policy as 79 tokens, counter/v2 as 105.
    store = _store(max_policy_tokens=78, counter_version="counter/v1")

    with pytest.raises(PolicyError) as raised:
        asyncio.run(_stage(store, Trace()).begin(RUN_ID))

    assert raised.value.code is PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert (raised.value.limit, raised.value.actual) == (78, 79)


def test_stage_lets_the_same_policy_through_on_a_v3_run() -> None:
    trace = Trace()
    stage = _stage(_store("v3", max_policy_tokens=_POLICY_TOKENS - 1), trace)

    asyncio.run(stage.begin(RUN_ID))

    assert stage.snapshot_for_llm is None
    assert "error" not in trace.steps[0][2]


def test_a_v4_run_over_the_policy_budget_ends_before_the_diff_and_any_model_call() -> None:
    events: list[str] = []
    github = CountingGitHub()
    stage = _stage(_store(max_policy_tokens=_POLICY_TOKENS - 1), Trace())

    with pytest.raises(PolicyError) as raised:
        asyncio.run(_processor(_binding(), events, github, stage=stage).prepare(RUN_ID))

    assert raised.value.code is PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert events == []
    assert github.reads == []


_META = PullRequestMeta("PR", None, "octo", "feature", "main", (), 1, 1, 0, False, False)


@dataclass
class Pipeline:
    binding: ReviewBinding | None
    snapshot: PolicySnapshot | None = None
    calls: list[str] = field(default_factory=list)
    contexts: list[ReviewContext] = field(default_factory=list)

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        self.calls.append("prepare")
        return GeneratedConventions(
            CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {}), None, False
        )

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="system",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=(),
            omitted_files=(),
            policy_snapshot=self.snapshot,
            review_binding=self.binding,
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        self.calls.append("pull request meta")
        return _META

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        self.calls.append("draft review")
        self.contexts.append(context)
        return {}

    async def execute(self, run_id: UUID, model_output: object) -> bool:
        return True


def _execute(pipeline: Pipeline) -> bool:
    return asyncio.run(ExecuteReviewRun(pipeline, pipeline, pipeline, pipeline).execute(RUN_ID))


@pytest.mark.parametrize(
    ("store", "message"),
    [
        (None, "no policy snapshot"),
        (_store(conventions=None), "not ready for the LLM"),
        # The run read names the prompt the run pinned; a snapshot pinning another one
        # never reaches the model.
        (_store(review_id=_OTHER_REVIEW), "pins another review prompt"),
    ],
    ids=["no-snapshot", "null-pin", "pins-mismatch"],
)
def test_execute_review_refuses_an_unservable_snapshot_path_run_before_the_review_model(
    store: Store | None, message: str
) -> None:
    pipeline = Pipeline(_binding(), snapshot=None if store is None else _snapshot(store))

    with pytest.raises(PolicyError) as raised:
        _execute(pipeline)

    assert raised.value.code is _INTEGRITY
    assert message in str(raised.value)
    # Conventions already ran (without a stage, the processor refuses before them); the PR
    # read and the review model are never reached.
    assert pipeline.calls == ["prepare"]


def test_execute_review_refuses_a_v4_run_of_an_unsupported_provider_before_the_review_model() -> (
    None
):
    pipeline = Pipeline(_binding("v4", "gitlab"), snapshot=_snapshot(_store()))

    with pytest.raises(PolicyError) as raised:
        _execute(pipeline)

    assert raised.value.code is _INTEGRITY
    assert "does not serve the provider 'gitlab'" in str(raised.value)
    assert pipeline.calls == ["prepare"]


@pytest.mark.parametrize(
    "binding",
    [_binding("v3"), _binding("v3", "gitlab"), _binding("v4", key="review.conventions")],
    ids=["v3", "v3-gitlab", "conventions-key-v4"],
)
def test_execute_review_keeps_a_legacy_run_without_a_snapshot(binding: ReviewBinding) -> None:
    # Even a ready snapshot that pins the bound prompt is not given to a legacy run.
    pipeline = Pipeline(binding, snapshot=_snapshot(_store(binding.review_system_version)))

    assert _execute(pipeline) is True

    assert pipeline.calls == ["prepare", "pull request meta", "draft review"]
    assert pipeline.contexts[0].policy_snapshot is None

"""Compose the durable inputs, model request, and review publication for one run."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.findings_post_processor import (
    FindingsPostProcessor,
    ProcessedFinding,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import CallManifest, PolicySnapshot
from app.modules.reviews.application.prompt_budget import (
    token_counter_for,
    validate_execution_profile_versions,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
)
from app.modules.reviews.application.rag_index import has_retrieved_section
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    filter_verified_candidates,
    verify_verifier_mandatory_budget,
)
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    NoReviewableText,
    RetrievedBlock,
    ReviewContextPayload,
    snapshot_block,
    snapshot_retrieved,
)
from app.modules.reviews.application.review_output import (
    DroppedCandidateRecord,
    InvalidReviewOutput,
    ReviewFinding,
    ReviewFindingV2,
    ReviewOutputHandler,
    VerifiedReviewOutput,
    incomplete_review_paths,
    validate_v2_attribution,
    validate_v2_coordinates,
)
from app.modules.reviews.application.review_path import ReviewBinding, snapshot_for_review
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.store_review_output import NoModelAnswer
from app.modules.reviews.application.verdict import SeverityThresholdValue

_LOGGER = logging.getLogger(__name__)
# R2: the payload levels shown as excerpts; L1 is the changed files themselves.
_EXCERPT_LEVELS = frozenset({ContextLevel.L2, ContextLevel.L3, ContextLevel.L4})


@dataclass(frozen=True)
class ReviewPromptInput:
    """All persisted, revision-pinned data required for a model prompt."""

    system: str
    rules: tuple[ReviewRule, ...]
    agents_md: str | None
    conventions: RepoConventions
    changed_files: tuple[ChangedFile, ...]
    omitted_files: tuple[str, ...]
    policy_snapshot: PolicySnapshot | None = None
    severity_threshold: SeverityThresholdValue = "all"
    # The review prompt the run pinned; None for a source that reads no pin (tests, eval).
    review_binding: ReviewBinding | None = None


@dataclass(frozen=True)
class ReviewReply:
    """A snapshot-path answer with what its call was given (R10, T-a).

    The review adapter is the only fit point: ``context`` is the fitted context the
    accepted call was sent with and ``manifest`` that call's manifest (base, repair or
    fallback), which attribution and the verifier check against.
    """

    output: Mapping[str, object]
    context: ReviewContext
    manifest: CallManifest


def build_deterministic_summary(
    final_findings: Sequence[ReviewFinding | ReviewFindingV2],
    *,
    changed_files: tuple[ChangedFile, ...] | list[ChangedFile],
    omitted_files: tuple[str, ...] | list[str] = (),
    below_threshold_count: int = 0,
) -> dict[str, object]:
    """Construct deterministic ReviewSummary per CONTEXT_AND_VERIFICATION_SPEC §8.2.

    Raw model summary is never published under any circumstances.
    """
    done_well = "Code analysis and policy verification were completed across all evaluated changes."

    if not final_findings:
        if below_threshold_count > 0:
            problem = (
                f"No verified issues meeting the severity threshold were found "
                f"({below_threshold_count} below threshold)."
            )
        else:
            problem = "No verified issues were found in the evaluated changes."
        effort = "none"
    else:
        severities = {f.severity for f in final_findings}
        if "critical" in severities:
            effort = "large"
        elif "high" in severities:
            effort = "medium"
        elif "medium" in severities or "low" in severities:
            effort = "small"
        else:
            effort = "none"

        categories = sorted({f.category for f in final_findings})
        cat_desc = ", ".join(categories)
        issue_word = "issue" if len(final_findings) == 1 else "issues"
        problem = (
            f"Identified {len(final_findings)} verified {issue_word} ({cat_desc}) "
            "in the reviewed changes."
        )

    return {
        "problem": problem,
        "done_well": done_well,
        "effort": effort,
    }


class ReviewPromptRepository(Protocol):
    """Read the immutable review context after diff and conventions are durable."""

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None: ...


class PullRequestMetaSource(Protocol):
    """Provider-neutral pull request metadata, including the commit context (#11)."""

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None: ...


class ReviewModel(PullRequestMetaSource, Protocol):
    """Request the review findings from the configured provider.

    ``draft_review`` receives the immutable context, not a rendered string: the LLM
    gateway (#33) fits it to its model's token budget, renders it with
    ``PromptBuilder`` as a system and a user message and returns the accepted JSON
    object, on the snapshot path as a ``ReviewReply`` with the fitted context and the
    accepted call's manifest. Run-attempt data (deadline, attempt, cost limit) is bound to the model
    adapter by the worker when it claims the run.
    """

    async def draft_review(
        self, *, context: ReviewContext
    ) -> Mapping[str, object] | str | bytes | ReviewReply: ...


class ReviewInputProcessor(Protocol):
    async def prepare(self, run_id: UUID) -> GeneratedConventions | bool | None: ...


class ReviewContextFilter(Protocol):
    """Which paths the policy stage of this attempt withheld from model inputs.

    It also hands over the attempt's policy snapshot, which it gives only to a run that
    takes the snapshot path.
    """

    @property
    def diff_old_sha(self) -> str | None: ...

    @property
    def head_sha(self) -> str | None: ...

    @property
    def snapshot_for_llm(self) -> PolicySnapshot | None: ...

    @property
    def payload(self) -> ReviewContextPayload | None: ...

    @property
    def ignored_file_paths(self) -> frozenset[str]:
        """Names of changed files of proven identity whose text the policy withholds.

        A mask, a built-in exclusion or the generated marker excluded them; an anchor on
        one of them is ``ignored_file`` (CVS §6.2 pass 2).
        """
        ...

    def excluded(self, path: str) -> bool: ...


class ExecuteReviewRun:
    """Finish an ordinary run after its diff and conventions have been saved.

    Database reads are short-lived and every model/provider call occurs after
    they complete. ``PublishReviewOutput`` owns its own two write transactions.
    """

    def __init__(
        self,
        processor: ReviewInputProcessor,
        repository: ReviewPromptRepository,
        model: ReviewModel,
        publisher: ReviewOutputHandler,
        context_filter: ReviewContextFilter | None = None,
        reflection_pass: ReflectionPass | None = None,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._model = model
        self._publisher = publisher
        self._context_filter = context_filter
        self._reflection_pass = reflection_pass

    async def execute(self, run_id: UUID) -> bool:
        try:
            conventions = await self._processor.prepare(run_id)
        except NoReviewableText:
            # CVS §2.2: the stage recorded a change of technical skips only. Nothing is shown
            # to a model: the backend result (coverage none) is published (#130 F5).
            return await self._publisher.execute(run_id, NoModelAnswer())
        if not isinstance(conventions, GeneratedConventions):
            return False
        prompt_input = await self._repository.get_review_prompt_input(run_id, conventions)
        if prompt_input is None:
            return False
        if self._context_filter is not None and prompt_input.policy_snapshot is None:
            # Lock 1: the stage's snapshot of this attempt, None on the legacy path.
            prompt_input = replace(
                prompt_input, policy_snapshot=self._context_filter.snapshot_for_llm
            )
        if prompt_input.review_binding is not None:
            # The stage chose the path before the conventions call; checked again here, so a
            # legacy run never gets a snapshot and a snapshot-path run without a usable one
            # never reaches the model.
            prompt_input = replace(
                prompt_input,
                policy_snapshot=snapshot_for_review(
                    prompt_input.review_binding, prompt_input.policy_snapshot
                ),
            )
        meta = await self._model.get_pull_request_meta(run_id)
        if meta is None:
            return False
        changed_files, omitted_files = prompt_input.changed_files, prompt_input.omitted_files
        if self._context_filter is not None:
            # An ignored file is neither shown nor named: it is dropped from both lists.
            excluded = self._context_filter.excluded
            changed_files = tuple(file for file in changed_files if not excluded(file.path))
            omitted_files = tuple(path for path in omitted_files if not excluded(path))
            if meta.diff_old_sha is None and self._context_filter.diff_old_sha is not None:
                meta = replace(meta, diff_old_sha=self._context_filter.diff_old_sha)
            if prompt_input.policy_snapshot is not None and self._context_filter.head_sha:
                # The stored diff and the excerpts were read at the head of the attempt's
                # diff map; a push after it must not relabel them (the live meta is read
                # after the conventions call). Legacy keeps the live head.
                meta = replace(meta, head_sha=self._context_filter.head_sha)
        context_blocks: tuple[ContextBlock, ...] = ()
        retrieved: tuple[RetrievedBlock, ...] = ()
        retrieved_section = False
        excluded_files = 0
        payload = None if self._context_filter is None else self._context_filter.payload
        if prompt_input.policy_snapshot is not None:
            # The section follows the bound prompt version, not the payload: a failed build
            # shows it empty.
            bindings = prompt_input.policy_snapshot.prompt_bindings
            retrieved_section = has_retrieved_section(
                bindings.review_system_key, bindings.review_system_version
            )
        if prompt_input.policy_snapshot is not None and payload is not None:
            # Every excerpt by the id the prompt, the manifest and the evidence share.
            context_blocks = tuple(
                snapshot_block(block) for block in payload.blocks if block.level in _EXCERPT_LEVELS
            )
            if retrieved_section:
                retrieved = tuple(snapshot_retrieved(block) for block in payload.retrieved)
            # P6-4: the ignored files stay counted, never named (RULES_FORMAT_SPEC §5.3).
            excluded_files = payload.ignored_files
        context = ReviewContext(
            system=prompt_input.system,
            rules=prompt_input.rules,
            agents_md=prompt_input.agents_md,
            conventions=prompt_input.conventions,
            pr_meta=meta,
            changed_files=changed_files,
            omitted_files=omitted_files,
            policy_snapshot=prompt_input.policy_snapshot,
            severity_threshold=prompt_input.severity_threshold,
            context_blocks=context_blocks,
            retrieved=retrieved,
            retrieved_section=retrieved_section,
            excluded_files=excluded_files,
        )

        if prompt_input.policy_snapshot is not None:
            if self._reflection_pass is None:
                raise RunFailure(
                    "internal_error",
                    "Reflection pass is required when policy snapshot is present",
                )
            if not prompt_input.policy_snapshot.is_ready_for_llm:
                raise PolicyError(
                    PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                    "Policy snapshot is not ready for LLM call",
                )
            validate_execution_profile_versions(prompt_input.policy_snapshot.execution_profile)
            counter = token_counter_for(
                prompt_input.policy_snapshot.execution_profile.counter_version
            )
            # The verifier route's window caps the mandatory verifier budget as well.
            verify_verifier_mandatory_budget(
                context,
                counter=counter,
                max_send_budget=self._reflection_pass.max_prompt_tokens(),
            )
            # R10: the review model fits, guards and manifests the call once; its reply
            # carries the fitted context and the accepted call's manifest.

        reply = await self._model.draft_review(context=context)
        reviewer_manifest: CallManifest | None = None
        model_output: Mapping[str, object] | str | bytes
        if isinstance(reply, ReviewReply):
            context, reviewer_manifest, model_output = reply.context, reply.manifest, reply.output
        else:
            # The legacy answer. On the snapshot path only a test fake answers so (the
            # gateway adapter always replies with ReviewReply): its fit and pre-send guards
            # did not run, and attribution checks a manifest built from the given context.
            if context.policy_snapshot is not None:
                _LOGGER.warning(
                    "Snapshot-path review answered without a ReviewReply: the fit and the "
                    "pre-send guards of the review adapter did not run run_id=%s",
                    run_id,
                )
            model_output = reply
        model_incomplete_paths = (
            model_output.incomplete_paths if isinstance(model_output, VerifiedReviewOutput) else ()
        )
        if self._reflection_pass is not None and context.policy_snapshot is not None:
            model_output = await self._run_reflection_pass(
                model_output,
                context,
                run_id=run_id,
                reviewer_manifest=reviewer_manifest,
                original_files=changed_files,
            )
        verified = model_output if isinstance(model_output, VerifiedReviewOutput) else None
        incomplete_paths = tuple(
            sorted(
                set(incomplete_review_paths(changed_files, context.changed_files))
                | set(model_incomplete_paths)
                | set(verified.incomplete_paths if verified is not None else ())
            )
        )
        try:
            answer_payload = (
                dict(model_output)
                if isinstance(model_output, Mapping)
                else json.loads(model_output)
            )
        except (ValueError, TypeError) as exc:
            raise InvalidReviewOutput("Malformed review model JSON output") from exc
        if not isinstance(answer_payload, dict):
            raise InvalidReviewOutput("Review model output must be an object")
        model_output = VerifiedReviewOutput(
            answer_payload,
            dropped_findings=verified.dropped_findings if verified is not None else (),
            severity_threshold=verified.severity_threshold if verified is not None else None,
            allowed_v2_rules=verified.allowed_v2_rules if verified is not None else None,
            verify_request=verified.verify_request if verified is not None else None,
            verify_response=verified.verify_response if verified is not None else None,
            incomplete_paths=incomplete_paths,
        )
        return await self._publisher.execute(run_id, model_output)

    async def _run_reflection_pass(
        self,
        model_output: Mapping[str, object] | str | bytes,
        context: ReviewContext,
        *,
        run_id: UUID,
        reviewer_manifest: CallManifest | None = None,
        original_files: tuple[ChangedFile, ...],
    ) -> VerifiedReviewOutput:
        assert self._reflection_pass is not None
        assert context.policy_snapshot is not None

        if isinstance(model_output, (str, bytes)):
            try:
                data = json.loads(model_output)
            except json.JSONDecodeError as exc:
                raise InvalidReviewOutput(f"Malformed review model JSON output: {exc}") from exc
        elif isinstance(model_output, Mapping):
            data = dict(model_output)
        else:
            raise InvalidReviewOutput(f"Unexpected review model output type: {type(model_output)}")

        if not isinstance(data, dict):
            raise InvalidReviewOutput(f"Expected dict for review model output, got {type(data)}")

        raw_findings = data.get("findings")
        if raw_findings is None or not isinstance(raw_findings, list):
            raise InvalidReviewOutput("Review model output missing 'findings' list")

        if len(raw_findings) > 10:
            raise InvalidReviewOutput("Draft review exceeded maximum of 10 findings")

        allowed_rules_set: set[str] = set()
        if context.policy_snapshot.rules.is_present:
            allowed_rules_set.add(".review/rules.md")
        if context.policy_snapshot.agents.is_present:
            allowed_rules_set.add("AGENTS.md")
        allowed_v2_rules = frozenset(allowed_rules_set)
        severity_threshold: SeverityThresholdValue = context.severity_threshold

        if reviewer_manifest is None:
            reviewer_manifest = PromptBuilder().build_manifest(
                context, call_id=uuid.uuid4(), stage="review"
            )

        # Names of changed files the policy stage proved ignored: their text is never shown.
        ignored_paths = (
            frozenset() if self._context_filter is None else self._context_filter.ignored_file_paths
        )
        _dropped_findings: list[DroppedCandidateRecord] = []
        candidates: list[CandidateFindingItem] = []
        cand_positions: dict[int, int] = {}
        for idx, item in enumerate(raw_findings):
            cand_id = str(uuid.uuid4())
            try:
                if isinstance(item, ReviewFindingV2):
                    finding = item
                elif isinstance(item, dict):
                    if "evidence_refs" not in item:
                        raise InvalidReviewOutput(
                            f"Candidate finding at index {idx} missing required evidence_refs"
                        )
                    finding = ReviewFindingV2.model_validate(item)
                elif hasattr(item, "model_dump"):
                    d = item.model_dump()
                    if "evidence_refs" not in d:
                        raise InvalidReviewOutput(
                            f"Candidate finding at index {idx} missing required evidence_refs"
                        )
                    finding = ReviewFindingV2.model_validate(d)
                else:
                    raise InvalidReviewOutput(
                        f"Unsupported finding type in draft review: {type(item)}"
                    )
            except InvalidReviewOutput:
                raise
            except ValidationError as err:
                sanitized_errors = "; ".join(
                    f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in err.errors()
                )
                raise InvalidReviewOutput(
                    f"Malformed candidate finding at index {idx}: {sanitized_errors}"
                ) from err
            except ValueError as err:
                raise InvalidReviewOutput(
                    f"Malformed candidate finding at index {idx}: {str(err)[:200]}"
                ) from err

            # Pass 2: Coordinates validation against fitted context (CVS §6.2 :701)
            valid_coords, coord_err = validate_v2_coordinates(
                finding,
                context.changed_files,
                original_files=original_files,
                ignored_paths=ignored_paths,
            )
            if not valid_coords:
                _LOGGER.info(
                    "Dropping candidate finding %s due to coordinate failure (%s)",
                    cand_id,
                    coord_err,
                )
                _dropped_findings.append(
                    DroppedCandidateRecord(
                        finding=finding,
                        drop_reason=coord_err or "line_absent",
                        position=idx,
                    )
                )
                continue

            # Pass 3: Validate attribution against manifest.
            # Unshown evidence refs drop only this candidate (CVS §6.2 :702, :729-730)
            valid_attr, attr_err = validate_v2_attribution(
                finding,
                reviewer_manifest,
                pr_meta=context.pr_meta,
                policy_snapshot=context.policy_snapshot,
            )
            if not valid_attr:
                _LOGGER.info(
                    "Dropping candidate finding %s due to unshown evidence (%s)",
                    cand_id,
                    attr_err,
                )
                drop_code = attr_err.split(":")[0].strip() if attr_err else "evidence_not_shown"
                _dropped_findings.append(
                    DroppedCandidateRecord(
                        finding=finding,
                        drop_reason=drop_code,
                        position=idx,
                    )
                )
                continue

            cand_positions[id(finding)] = idx
            candidates.append(CandidateFindingItem(candidate_id=cand_id, finding=finding))

        # Pass 4: Coordinates, lint filter, and dedup BEFORE verifier (CVS §6.2 :704)
        post_processor = FindingsPostProcessor.from_default_patterns()
        processed_candidates = [
            ProcessedFinding(c.finding, cand_positions.get(id(c.finding), idx), "inline")
            for idx, c in enumerate(candidates)
        ]
        kept_candidates, lint_dropped = post_processor._drop_lint_patterns(processed_candidates)
        for item in lint_dropped:
            _dropped_findings.append(
                DroppedCandidateRecord(
                    finding=item.finding,
                    drop_reason="lint_pattern",
                    position=item.position,
                )
            )
        kept_candidates, duplicate_dropped = post_processor._deduplicate(kept_candidates)
        for item in duplicate_dropped:
            _dropped_findings.append(
                DroppedCandidateRecord(
                    finding=item.finding,
                    drop_reason="duplicate",
                    position=item.position,
                )
            )

        kept_findings_set = {id(p.finding) for p in kept_candidates}
        filtered_candidates = [c for c in candidates if id(c.finding) in kept_findings_set]

        # If empty (0 raw candidates or 0 after filters), bypass verifier (0 calls)
        if not filtered_candidates:
            data["findings"] = []
            data["summary"] = build_deterministic_summary(
                (),
                changed_files=context.changed_files,
                omitted_files=context.omitted_files,
            )
            return VerifiedReviewOutput(
                data,
                dropped_findings=tuple(_dropped_findings),
                severity_threshold=severity_threshold,
                # R12: the v2 rule allow-list belongs to verified findings; no verifier call
                # happened, so publication keeps its own attribution check.
                allowed_v2_rules=None,
            )

        # #247 p3 (RULES §8.2): a wrong attribution is removed before the verifier, so the
        # finding reaches it as a candidate without that claim and is published only on its
        # decision, never as a silently confirmed generic finding.
        rule_names = frozenset(rule.name for rule in context.rules)
        repaired_candidates: list[CandidateFindingItem] = []
        for cand in filtered_candidates:
            position = cand_positions.get(id(cand.finding), 0)
            repaired = post_processor._repair_attribution(
                ProcessedFinding(cand.finding, position, "inline"),
                rule_names,
                allowed_v2_rules=allowed_v2_rules,
            ).finding
            # CONTRACT: the repair copies the finding it gets (model_copy keeps ReviewFindingV2).
            assert isinstance(repaired, ReviewFindingV2)
            # CONTRACT: positions are keyed by id(); a repaired copy is a new object.
            cand_positions[id(repaired)] = position
            repaired_candidates.append(CandidateFindingItem(cand.candidate_id, repaired))
        filtered_candidates = repaired_candidates

        # Pass 5: Verifier invocation
        record = await self._reflection_pass.verify_recorded(
            candidates=tuple(filtered_candidates), context=context
        )
        decisions = record.decisions
        supported, rejected = filter_verified_candidates(
            candidates=tuple(filtered_candidates), decisions=decisions
        )
        for cand, status, _reason in rejected:
            _dropped_findings.append(
                DroppedCandidateRecord(
                    finding=cand.finding,
                    drop_reason=status,
                    position=cand_positions.get(id(cand.finding), 0),
                )
            )

        _LOGGER.info(
            "Reflection pass completed for run %s (call %s, verifier call %s): "
            "total=%d, supported=%d, rejected=%d. Decisions: %s",
            run_id,
            reviewer_manifest.call_id,
            record.accepted_call_id,
            len(filtered_candidates),
            len(supported),
            len(rejected),
            [(d.candidate_id, d.status) for d in decisions],
        )

        # Pass 6: Severity threshold after verifier (CVS §6.2 :705)
        supported_processed = [
            ProcessedFinding(cand.finding, cand_positions.get(id(cand.finding), idx), "inline")
            for idx, cand in enumerate(supported)
        ]
        surviving_processed, below_threshold_processed = post_processor._drop_below_threshold(
            supported_processed, severity_threshold
        )
        for item in below_threshold_processed:
            _dropped_findings.append(
                DroppedCandidateRecord(
                    finding=item.finding,
                    drop_reason="severity_threshold",
                    position=item.position,
                )
            )
        final_findings = [p.finding for p in surviving_processed]

        data["findings"] = [f.model_dump() for f in final_findings]
        # Deterministic summary is ALWAYS built from post-threshold supported findings (CVS §8.2)
        data["summary"] = build_deterministic_summary(
            final_findings,
            changed_files=context.changed_files,
            omitted_files=context.omitted_files,
            below_threshold_count=len(below_threshold_processed),
        )

        return VerifiedReviewOutput(
            data,
            dropped_findings=tuple(_dropped_findings),
            severity_threshold=severity_threshold,
            allowed_v2_rules=allowed_v2_rules,
            # llm.verify (PIPELINE_SPEC:177): manifest_id is the stored manifest of the
            # accepted verifier call, null without a manifest store; reason is never stored.
            verify_request={
                "candidate_ids": list(record.candidate_ids),
                "accepted_call_id": (
                    None if record.accepted_call_id is None else str(record.accepted_call_id)
                ),
                "manifest_id": None if record.manifest_id is None else str(record.manifest_id),
            },
            verify_response={
                "decisions": [
                    decision.model_dump(mode="json", exclude={"reason"}) for decision in decisions
                ]
            },
        )

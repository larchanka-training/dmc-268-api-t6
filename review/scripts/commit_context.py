"""Validate frozen PR-prefix evidence separately from gold-case identity binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.infrastructure.github_link_header import has_next_relation
from app.modules.reviews.application.commit_prefix_limits import (
    COMMIT_PAGE_COUNT,
    COMMIT_PAGE_SIZE,
    MAX_COMMIT_MESSAGE_CHARS,
    MAX_COMMIT_MESSAGES,
    MAX_COMMIT_PAGES,
    MAX_COMMIT_RESPONSE_BYTES,
    CommitEncoding,
    commit_prefix_incomplete,
)
from app.modules.reviews.application.prompt_builder import shorten_commit_message
from app.modules.reviews.application.vcs_diff import CommitMessagePrefix

_SHA = r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$"
_PR_URL = r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$"


class CommitContextError(ValueError):
    """Frozen evidence or its corpus binding is invalid, with a safe diagnostic."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Retrieval(_StrictModel):
    per_page: int = Field(ge=COMMIT_PAGE_SIZE, le=COMMIT_PAGE_SIZE)
    max_pages: int = Field(ge=MAX_COMMIT_PAGES, le=MAX_COMMIT_PAGES)
    max_messages: int = Field(ge=MAX_COMMIT_MESSAGES, le=MAX_COMMIT_MESSAGES)
    max_message_chars: int = Field(ge=MAX_COMMIT_MESSAGE_CHARS, le=MAX_COMMIT_MESSAGE_CHARS)
    max_response_bytes: int = Field(ge=MAX_COMMIT_RESPONSE_BYTES, le=MAX_COMMIT_RESPONSE_BYTES)
    accept_encoding: CommitEncoding


class _Entry(_StrictModel):
    sha: str = Field(pattern=_SHA)
    message: str

    @field_validator("message")
    @classmethod
    def encodable_message(cls, value: str) -> str:
        value.encode("utf-8")
        return value


class _HeadEvidence(_StrictModel):
    verified_via: str
    before_sha: str = Field(pattern=_SHA)
    after_sha: str = Field(pattern=_SHA)
    state: Literal["closed"]
    merged: bool
    total_commits: int = Field(ge=1)


class _Page(_StrictModel):
    page: int = Field(ge=1, le=COMMIT_PAGE_COUNT)
    verified_via: str
    response_date: AwareDatetime
    response_bytes: int = Field(ge=0, le=MAX_COMMIT_RESPONSE_BYTES)
    content_encoding: CommitEncoding | None
    link_header: str | None
    next_relation: bool
    entries: list[_Entry] = Field(max_length=COMMIT_PAGE_SIZE)

    @model_validator(mode="after")
    def coherent_link(self) -> _Page:
        has_next = has_next_relation(self.link_header)
        if self.next_relation != has_next:
            raise ValueError("inconsistent next relation")
        return self


class _Record(_StrictModel):
    case_id: str = Field(pattern=r"^(?:CLEAN|LOG|RES|SEC|SYN)-[0-9]{2}$")
    source_url: str = Field(pattern=_PR_URL)
    revision: str = Field(pattern=_SHA)
    head_evidence: _HeadEvidence
    pages: list[_Page] = Field(min_length=1, max_length=COMMIT_PAGE_COUNT)
    incomplete: bool

    @model_validator(mode="after")
    def coherent_prefix(self) -> _Record:
        repository, number = self.source_url.removeprefix("https://github.com/").split("/pull/")
        api = f"https://api.github.com/repos/{repository}/pulls/{number}"
        head = self.head_evidence
        if (
            head.verified_via != api
            or head.before_sha != self.revision
            or head.after_sha != self.revision
        ):
            raise ValueError("inconsistent head evidence")
        if len(self.pages) < COMMIT_PAGE_COUNT and len(self.pages[-1].entries) == COMMIT_PAGE_SIZE:
            raise ValueError("full first page needs the second numeric page")
        for index, page in enumerate(self.pages, start=1):
            if page.page != index or page.verified_via != (
                f"{api}/commits?per_page={COMMIT_PAGE_SIZE}&page={index}"
            ):
                raise ValueError("inconsistent numeric page evidence")
            if index < len(self.pages) and len(page.entries) != COMMIT_PAGE_SIZE:
                raise ValueError("page after a short prefix page")
        entries = [entry for page in self.pages for entry in page.entries]
        if len({entry.sha for entry in entries}) != len(entries):
            raise ValueError("repeated commit SHA")
        last = self.pages[-1]
        incomplete = commit_prefix_incomplete(
            has_next=last.next_relation,
            page=last.page,
            batch_length=len(last.entries),
            remaining=MAX_COMMIT_MESSAGES - (len(entries) - len(last.entries)),
        )
        if self.incomplete != incomplete or head.total_commits < len(entries):
            raise ValueError("inconsistent prefix tail evidence")
        if not incomplete and (
            len(entries) != head.total_commits or entries[-1].sha != self.revision
        ):
            raise ValueError("complete prefix disagrees with head evidence")
        return self

    def prefix(self) -> CommitMessagePrefix:
        return CommitMessagePrefix(
            tuple(
                shorten_commit_message(entry.message)
                for page in self.pages
                for entry in page.entries
            ),
            self.incomplete,
        )


class CommitContextSidecar(_StrictModel):
    schema_version: int = Field(ge=2, le=2)
    context_kind: Literal["bounded_pr_prefix"]
    description: str
    verification_started_at: AwareDatetime
    verification_finished_at: AwareDatetime
    retrieval: _Retrieval
    records: list[_Record]

    @model_validator(mode="after")
    def coherent_capture(self) -> CommitContextSidecar:
        if self.verification_started_at > self.verification_finished_at:
            raise ValueError("inconsistent verification interval")
        if len({record.case_id for record in self.records}) != len(self.records):
            raise ValueError("duplicate commit context record")
        if any(
            not self.verification_started_at <= page.response_date <= self.verification_finished_at
            for record in self.records
            for page in record.pages
        ):
            raise ValueError("page response outside verification interval")
        return self


def parse_commit_context(path: Path) -> CommitContextSidecar:
    """Validate the entire evidence document without consulting corpus identities."""
    try:
        return CommitContextSidecar.model_validate_json(path.read_bytes())
    except OSError:
        raise CommitContextError("cannot read commit context sidecar") from None
    except (ValueError, RecursionError):
        raise CommitContextError("malformed commit context sidecar") from None


class _IdentitySource(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Literal["real", "synthetic"]
    url: str | None = None
    revision: str | None = None


class _Identity(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    source: _IdentitySource


def load_canonical_identities(repo: Path, paths: Sequence[str]) -> list[dict[str, Any]]:
    """Read only pinned case identity fields, independent of patches and corpus schema."""
    identities = []
    try:
        for relative in paths:
            path = repo / relative
            if path.is_symlink():
                raise ValueError("symlink canonical identity")
            identity = _Identity.model_validate_json(path.read_bytes())
            if identity.id != path.parent.name:
                raise ValueError("canonical case identity disagrees with its path")
            identities.append(identity.model_dump())
        if len({identity["id"] for identity in identities}) != len(identities):
            raise ValueError("duplicate canonical case identity")
    except (OSError, ValueError, RecursionError):
        raise CommitContextError("invalid canonical commit context identities") from None
    return identities


def bind_commit_context(
    sidecar: CommitContextSidecar,
    active_cases: Sequence[Mapping[str, Any]],
    cohort_cases: Sequence[Mapping[str, Any]],
) -> dict[str, CommitMessagePrefix]:
    """Bind parsed evidence to the cohort and selected cases before calls or staging."""
    snapshots = {snapshot.case_id: snapshot for snapshot in sidecar.records}
    cohort = {case["id"]: case for case in cohort_cases}
    for case_id, snapshot in snapshots.items():
        case = cohort.get(case_id)
        if case is None:
            raise CommitContextError("unexpected commit context record")
        source = case["source"]
        if source["kind"] != "real":
            raise CommitContextError("synthetic case has commit context")
        if snapshot.source_url != source["url"] or snapshot.revision != source["revision"]:
            raise CommitContextError("mismatched commit context for real case")
    if any(
        case["source"]["kind"] == "real" and case["id"] not in snapshots for case in cohort_cases
    ):
        raise CommitContextError("missing commit context for real case")
    contexts: dict[str, CommitMessagePrefix] = {}
    for case in active_cases:
        active_snapshot = snapshots.get(case["id"])
        source = case["source"]
        if source["kind"] != "real":
            if active_snapshot is not None:
                raise CommitContextError("synthetic case has commit context")
            contexts[case["id"]] = CommitMessagePrefix(())
        elif active_snapshot is None:
            raise CommitContextError("missing commit context for real case")
        elif (
            active_snapshot.source_url != source["url"]
            or active_snapshot.revision != source["revision"]
        ):
            raise CommitContextError("mismatched commit context for real case")
        else:
            contexts[case["id"]] = active_snapshot.prefix()
    return contexts

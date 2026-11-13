"""The envelopes, contexts and parsers the review.system prompt tests (v4, v5, v7) share."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    ExecutionProfile,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.prompt_builder import (
    DEFAULT_BACKEND_V1_CHECKSUM,
    ChangedFile,
    DiffLine,
    FileStatus,
    LineType,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
    review_rule_from_stored,
)
from app.modules.reviews.application.rag_index import has_retrieved_section
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    Representation,
    RetrievedBlock,
    snapshot_retrieved,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "review/prompts"
V3_SHA256 = "eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452"
# The reviewed v4 bytes; a text change needs a new version, never a new digest here.
V4_SHA256 = "5738227c7352228d29975e0512b5b5d3354eff31c9d161ca72086a776e9c498f"

V5_PATH = PROMPTS / "review.system.v5.md"
INPUT_TAGS = [
    "repository_rules",
    "project_conventions_document",
    "service_defaults",
    "repo_conventions",
    "pr_meta",
    "changed_files",
    "context_excerpts",
    "retrieved_excerpts",
    "omitted_files",
    "excluded_files",
]
_BLOCK_START = re.compile(rf"<({'|'.join(INPUT_TAGS)})[ >]")

HEAD = "7c3e9a14b2d85f06e1a4c9b7d3f20e58a6c1b947"
REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000401")
NO_CONVENTIONS = RepoConventions((), ())
META = PullRequestMeta(
    "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False, head_sha=HEAD
)
BASE = "3b9e0c71d4a85f26e0b7c4d9a1f36e28b5c0d417"


def _present(path: str, text: str) -> PolicyDocument:
    raw = text.encode("utf-8")
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.PRESENT,
        blob_oid="d" * 40,
        raw_sha256=sha256(raw).hexdigest(),
        text_sha256=sha256(raw).hexdigest(),
        text=text,
        byte_count=len(raw),
        line_count=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
    )


def _missing(path: str) -> PolicyDocument:
    return PolicyDocument(path, DocumentPresence.MISSING, None, None, None, None, None, None)


def _snapshot(rules: PolicyDocument, agents: PolicyDocument) -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000402"),
        run_id=UUID("00000000-0000-0000-0000-000000000403"),
        repository_id=REPOSITORY_ID,
        policy_base_sha="b" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000404"),
        defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM,
        # The envelope never renders the bindings; they only make the snapshot a v4 one.
        prompt_bindings=PromptBindings(
            review_system_checksum="4" * 64,
            conventions_checksum="2" * 64,
            verifier_checksum="2" * 64,
            review_system_version="v4",
            review_system_id=UUID("00000000-0000-0000-0000-000000000405"),
            conventions_id=UUID("00000000-0000-0000-0000-000000000406"),
            verifier_id=UUID("00000000-0000-0000-0000-000000000407"),
        ),
        execution_profile=ExecutionProfile.fast("review-model", "verifier-model"),
        policy_digest="d" * 64,
    )


def _file(path: str, status: FileStatus, *lines: tuple[int, LineType, str]) -> ChangedFile:
    return ChangedFile(path, status, tuple(DiffLine(n, kind, text) for n, kind, text in lines))


def _excerpt(
    block_id: str,
    level: ContextLevel,
    path: str,
    ranges: tuple[tuple[int, int], ...],
    text: str,
) -> ContextBlock:
    signature = level is ContextLevel.L4
    return ContextBlock(
        block_id=block_id,
        level=level,
        file_id=None,
        source=SourceRef(
            role=SourceRole.AST_EXCERPT if signature else SourceRole.SOURCE_EXCERPT,
            repository_id=REPOSITORY_ID,
            revision=HEAD,
            path=path,
            blob_sha=None,
            origin_paths=(),
            side=DiffSide.RIGHT,
            ranges=tuple(LineRange(start, end) for start, end in ranges),
            reason="imported_definition" if signature else "full_file",
        ),
        representation=Representation.SIGNATURE if signature else Representation.EXACT_LINES,
        text=text,
        tokens=len(text) // 4,
        truncated=False,
    )


def _context(
    files: tuple[ChangedFile, ...],
    *,
    rules: PolicyDocument | None = None,
    agents: PolicyDocument | None = None,
    defaults: tuple[ReviewRule, ...] = (),
    blocks: tuple[ContextBlock, ...] = (),
    conventions: RepoConventions = NO_CONVENTIONS,
    omitted: tuple[str, ...] = (),
    excluded: int = 0,
) -> ReviewContext:
    return ReviewContext(
        system="",
        rules=defaults,
        agents_md=None,
        conventions=conventions,
        pr_meta=META,
        changed_files=files,
        omitted_files=omitted,
        policy_snapshot=_snapshot(
            rules or _missing(RULES_DOCUMENT_PATH), agents or _missing(AGENTS_DOCUMENT_PATH)
        ),
        context_blocks=blocks,
        excluded_files=excluded,
    )


def _backend_defaults() -> tuple[ReviewRule, ...]:
    stored = json.loads((REPO_ROOT / "review/rules/default-backend.v1.json").read_text())
    return tuple(review_rule_from_stored(rule) for rule in stored["rules"])


def _error_handling() -> ReviewRule:
    return next(rule for rule in _backend_defaults() if rule.name == "Error Handling Standards")


_WITHDRAW = _file(
    "app/accounts/withdraw.py",
    "modified",
    (14, "context", "    async def execute(self, account: Account, amount: Decimal) -> None:"),
    (15, "added", "        if amount <= 0:"),
    (16, "added", "            raise InvalidAmount(amount)"),
    (17, "added", "        if account.balance > amount:"),
    (18, "added", "            raise InsufficientFunds(account.id)"),
    (19, "added", "        await self._ledger.debit(account.id, amount)"),
)
_RESUME = _file(
    "app/sessions/resume.py",
    "modified",
    (19, "context", "    async def execute(self, token: str) -> Session:"),
    (20, "added", "        payload = await read_session(self._redis, token)"),
    (21, "added", '        return Session.from_json(payload.decode("utf-8"))'),
)
_READ_SESSION = _excerpt(
    "L4:7c3e9a14b2d8:4f1d0b62a9e7c385",
    ContextLevel.L4,
    "app/sessions/store.py",
    ((27, 28),),
    "async def read_session(redis: Redis, token: str) -> bytes | None:\n"
    '    """Return the stored session payload, or None once it has expired."""',
)
_MONEY_TEST = _file(
    "tests/test_money.py",
    "modified",
    (12, "context", "def test_rounds_half_even() -> None:"),
    (13, "removed", '    assert round_money(Decimal("0.125")) == Decimal("0.13")'),
    (13, "added", '    assert round_money(Decimal("0.125")) == Decimal("0.12")'),
)
_EMPTY_AGENTS = PolicyDocument(
    AGENTS_DOCUMENT_PATH,
    DocumentPresence.PRESENT,
    "e" * 40,
    sha256(b"").hexdigest(),
    sha256(b"").hexdigest(),
    "",
    0,
    0,
)
_REFUND = _file(
    "app/billing/refund.py",
    "modified",
    (30, "context", "    async def execute(self, request: RefundRequest) -> RefundResult:"),
    (31, "added", "        amount = float(request.amount)"),
    (32, "added", "        return await self._payments.refund(request.payment_id, amount)"),
)
_GATEWAY = _file(
    "app/billing/gateway.py",
    "modified",
    (40, "context", "    async def refund(self, payment_id: str, amount: Decimal) -> bool:"),
    (41, "context", "        try:"),
    (42, "added", "            await self._client.refund(payment_id, str(amount))"),
    (43, "context", "        except httpx.HTTPError:"),
    (44, "added", '            logger.warning("refund failed for %s", payment_id)'),
    (45, "added", "        return True"),
)
_RECEIPTS = _file(
    "app/billing/receipts.py",
    "modified",
    (12, "added", '@router.get("/receipts/{name:path}")'),
    (13, "added", "async def download_receipt(name: str) -> FileResponse:"),
    (14, "added", "    return FileResponse(RECEIPTS_DIR / name)"),
)
_MONEY_RULES = "## Money\n- Money amounts are `Decimal`; never convert them to `float`.\n"

# The four examples of section 14, in order; the prompt shows blocks of exactly this output.
EXAMPLES: tuple[ReviewContext, ...] = (
    _context((_WITHDRAW,)),
    _context((_RESUME,), blocks=(_READ_SESSION,)),
    _context((_MONEY_TEST,), agents=_EMPTY_AGENTS),
    _context(
        (_REFUND, _GATEWAY, _RECEIPTS),
        rules=_present(RULES_DOCUMENT_PATH, _MONEY_RULES),
        defaults=(_error_handling(),),
    ),
)


def _golden() -> ReviewContext:
    """Every input tag at once: both documents, the backend defaults, L3, L4 and an exclusion."""
    context = _context(
        (
            _file("app/a.py", "modified", (1, "context", "a = 1"), (2, "added", "b = a")),
            _file("app/b.py", "added", (1, "added", "from app.c import c")),
        ),
        rules=_present(RULES_DOCUMENT_PATH, "# Rules\n- Keep handlers thin.\n"),
        agents=_present(AGENTS_DOCUMENT_PATH, "# AGENTS\nUse uv.\n"),
        defaults=_backend_defaults(),
        blocks=(
            _excerpt("L3:0123:1-2", ContextLevel.L3, "app/a.py", ((1, 2),), "a = 1\nb = a"),
            _excerpt(
                "L4:7c3e9a14b2d8:0011", ContextLevel.L4, "app/c.py", ((3, 3),), "def c(): ..."
            ),
        ),
        conventions=RepoConventions(("Use cases commit.",), ("Check the unit of work.",)),
        omitted=("app/huge.py",),
        excluded=1,
    )
    return replace(context, pr_meta=replace(META, commit_messages=("Add b",)))


def _v5(context: ReviewContext, retrieved: tuple[RetrievedBlock, ...] = ()) -> ReviewContext:
    """``context`` as a v5-bound run renders it: the section follows the pinned version."""
    snapshot = context.policy_snapshot
    assert snapshot is not None
    bindings = replace(
        snapshot.prompt_bindings, review_system_checksum="5" * 64, review_system_version="v5"
    )
    return replace(
        context,
        policy_snapshot=replace(snapshot, prompt_bindings=bindings),
        retrieved=retrieved,
        retrieved_section=has_retrieved_section("review.system", bindings.review_system_version),
    )


def _retrieved(
    path: str,
    revision: str,
    side: DiffSide,
    ranges: tuple[tuple[int, int], ...],
    text: str,
    *,
    signature: bool = False,
    definitions: tuple[SourceRef, ...] = (),
) -> RetrievedBlock:
    source = SourceRef(
        role=SourceRole.RETRIEVED_EXCERPT,
        repository_id=REPOSITORY_ID,
        revision=revision,
        path=path,
        blob_sha=None,
        origin_paths=(),
        side=side,
        ranges=tuple(LineRange(start, end) for start, end in ranges),
        reason="rag_broken_reference",
    )
    spans = ",".join(f"{start}-{end}" for start, end in ranges)
    # The stage's id, renamed as the snapshot path shows and cites it.
    return snapshot_retrieved(
        RetrievedBlock(
            block_id=f"RAG:{path}:{spans}",
            level=ContextLevel.RAG,
            source=source,
            representation=Representation.SIGNATURE if signature else Representation.EXACT_LINES,
            text=text,
            tokens=len(text) // 4,
            edge_kind=EdgeKind.IMPORTS,
            index_commit_sha="c" * 40,
            definitions=definitions,
        )
    )


def _blocks(rendered: str) -> list[tuple[str, str]]:
    """The top-level blocks of an input envelope: ``(tag, exact text)``, in order."""
    lines = rendered.split("\n")
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        match = _BLOCK_START.match(lines[index])
        assert match is not None, f"not a top-level tag: {lines[index]!r}"
        tag = match.group(1)
        end = index if lines[index].endswith(" />") else lines.index(f"</{tag}>", index)
        blocks.append((tag, "\n".join(lines[index : end + 1])))
        index = end + 1
    return blocks


def _front_matter(text: str) -> str:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    return match.group(1)


def _body(text: str) -> list[str]:
    return text.split("\n---\n", 1)[1].split("\n")

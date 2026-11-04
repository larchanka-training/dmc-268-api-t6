"""Tests for factual claim verification against visible code context (#445)."""

from __future__ import annotations

from app.modules.reviews.application.claim_verifier import (
    audit_candidate_claim,
)
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    VerificationDecision,
    filter_verified_candidates,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    ReviewFindingV2,
)


def _make_candidate(
    *,
    candidate_id: str = "cand-1",
    path: str = "src/tools/films.ts",
    line: int = 96,
    title: str = "Issue title",
    body: str = "Issue body",
    evidence_refs: list[EvidenceRef] | None = None,
) -> CandidateFindingItem:
    refs = (
        evidence_refs
        if evidence_refs is not None
        else [EvidenceRef(block_id=file_block_id(path), start_line=line, end_line=line)]
    )
    finding = ReviewFindingV2(
        path=path,
        line=line,
        start_line=None,
        severity="high",
        category="correctness",
        title=title,
        body=body,
        suggestion=None,
        confidence=0.9,
        rule_name=".review/rules.md",
        evidence_refs=refs,
    )
    return CandidateFindingItem(candidate_id=candidate_id, finding=finding)


class TestImportClaimVerification:
    def test_mcperror_not_imported_claim_is_contradicted_in_typescript(self) -> None:
        code = (
            "import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';\n"
            "import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js';\n"
            "import { z } from 'zod';\n"
            "\n"
            "export function getFilm() {\n"
            "  throw new McpError(ErrorCode.InvalidParams, 'error');\n"
            "}\n"
        )
        cand = _make_candidate(
            title="McpError is not imported in get_film tool",
            body=(
                "The get_film tool uses McpError on line 6, but neither is imported. "
                "Import from server/mcp.js."
            ),
            line=6,
        )
        result = audit_candidate_claim(cand, code.encode("utf-8"), "src/tools/films.ts")
        assert not result.is_valid
        assert result.status == "contradicted"
        assert "McpError" in (result.reason or "")

    def test_python_import_claim_is_contradicted_when_imported(self) -> None:
        code = (
            "from app.errors import ApiError, NotFoundError\n"
            "\n"
            "def handle():\n"
            "    raise ApiError('failed')\n"
        )
        cand = _make_candidate(
            path="app/views.py",
            line=4,
            title="ApiError not imported",
            body="ApiError is not imported in this module. Import it.",
        )
        result = audit_candidate_claim(cand, code.encode("utf-8"), "app/views.py")
        assert not result.is_valid
        assert result.status == "contradicted"
        assert "ApiError" in (result.reason or "")

    def test_truly_missing_import_claim_is_not_flagged_as_contradicted(self) -> None:
        code = "import os\n\ndef handle():\n    raise MissingCustomError('failed')\n"
        cand = _make_candidate(
            path="app/views.py",
            line=4,
            title="MissingCustomError not imported",
            body="MissingCustomError is not imported.",
        )
        result = audit_candidate_claim(cand, code.encode("utf-8"), "app/views.py")
        assert result.is_valid
        assert result.status == "supported"


class TestTryCatchClaimVerification:
    def test_missing_try_catch_claim_contradicted_when_inside_try_block(self) -> None:
        code = (
            "export async function getFilm(id: number) {\n"
            "  try {\n"
            "    const film = await client.getFilm(id);\n"
            "    return film;\n"
            "  } catch (error) {\n"
            "    return toErrorResult(error);\n"
            "  }\n"
            "}\n"
        )
        cand = _make_candidate(
            line=3,
            title="Unhandled error in get_film tool",
            body="get_film calls client.getFilm without a surrounding try/catch.",
        )
        result = audit_candidate_claim(cand, code.encode("utf-8"), "src/tools/films.ts")
        assert not result.is_valid
        assert result.status == "contradicted"
        assert "try/catch" in (result.reason or "")

    def test_missing_try_catch_valid_when_genuinely_outside_try(self) -> None:
        code = (
            "export async function getFilm(id: number) {\n"
            "  const film = await client.getFilm(id);\n"
            "  return film;\n"
            "}\n"
        )
        cand = _make_candidate(
            line=2,
            title="Unhandled error in get_film tool",
            body="get_film calls client.getFilm without a surrounding try/catch.",
        )
        result = audit_candidate_claim(cand, code.encode("utf-8"), "src/tools/films.ts")
        assert result.is_valid
        assert result.status == "supported"


class TestExternalComparativeClaims:
    def test_comparative_claim_about_other_tools_lacking_evidence_is_insufficient_context(
        self,
    ) -> None:
        cand = _make_candidate(
            title="Inconsistent parameter naming in get_starship",
            body=(
                "The tool uses id as the parameter name, while other tools in the "
                "codebase use idOrUrl."
            ),
            path="src/tools/starships.ts",
            line=31,
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=31, end_line=31
                )
            ],
        )
        result = audit_candidate_claim(cand, b"code", "src/tools/starships.ts")
        assert not result.is_valid
        assert result.status == "insufficient_context"
        assert "other tools" in (result.reason or "")

    def test_comparative_claim_with_proper_evidence_ref_is_retained(self) -> None:
        cand = _make_candidate(
            title="Inconsistent parameter naming in get_starship",
            body=(
                "The tool uses id as the parameter name, while other tools in the "
                "codebase use idOrUrl."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id="file-src-tools-people-ts-abc12345", start_line=10, end_line=15
                )
            ],
        )
        result = audit_candidate_claim(cand, b"code", "src/tools/starships.ts")
        assert result.is_valid
        assert result.status == "supported"


class TestFilterVerifiedCandidatesWithClaimAudit:
    def test_supported_decision_overridden_when_claim_is_contradicted(self) -> None:
        code = (
            "import { McpError } from '@modelcontextprotocol/sdk/types.js';\n"
            "export function test() { throw new McpError(1, 'err'); }\n"
        )
        cand = _make_candidate(
            candidate_id="cand-1",
            title="McpError is not imported",
            body="McpError is not imported in test function.",
            line=2,
        )
        decision = VerificationDecision(
            candidate_id="cand-1",
            status="supported",  # Verifier incorrectly said supported
            reason="Confirmed defect",
            evidence_refs=[EvidenceRef(block_id="file-src-test-ts-1", start_line=2, end_line=2)],
            requirement_refs=[],
        )
        supported, rejected = filter_verified_candidates(
            candidates=(cand,),
            decisions=(decision,),
            file_contents={"src/tools/films.ts": code.encode("utf-8")},
        )
        assert len(supported) == 0
        assert len(rejected) == 1
        assert rejected[0][1] == "contradicted"

    def test_true_findings_are_retained_as_supported(self) -> None:
        code = "export function request(url: string) { return fetch(url); }\n"
        cand = _make_candidate(
            candidate_id="cand-ssrf",
            title="URL origin not validated before outbound request",
            body=(
                "request builds a URL from pathOrUrl without validating that "
                "origin matches baseUrl."
            ),
            line=1,
        )
        decision = VerificationDecision(
            candidate_id="cand-ssrf",
            status="supported",
            reason="Origin validation missing",
            evidence_refs=[EvidenceRef(block_id="file-src-api-ts-1", start_line=1, end_line=1)],
            requirement_refs=[],
        )
        supported, rejected = filter_verified_candidates(
            candidates=(cand,),
            decisions=(decision,),
            file_contents={"src/tools/films.ts": code.encode("utf-8")},
        )
        assert len(supported) == 1
        assert len(rejected) == 0
        assert supported[0].candidate_id == "cand-ssrf"

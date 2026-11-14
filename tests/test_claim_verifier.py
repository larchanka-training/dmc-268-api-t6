"""Unit and ExecuteReviewRun integration tests for deterministic claim auditing (#445)."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.modules.reviews.application.ast_symbols import MAX_SOURCE_BYTES
from app.modules.reviews.application.claim_verifier import (
    ClaimAuditResult,
    audit_candidate_claim,
    collect_context_blocks,
    extract_full_source_bytes,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationDecision,
    filter_verified_candidates,
)
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    ContextStatus,
    Representation,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    ReviewFindingV2,
    VerifiedReviewOutput,
)
from app.modules.reviews.infrastructure.claim_syntax import TreeSitterClaimSyntaxInspector
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

REPO_ID = UUID("00000000-0000-0000-0000-000000004451")
RUN_ID = UUID("00000000-0000-0000-0000-000000004452")
INSPECTOR = TreeSitterClaimSyntaxInspector()

# Authentic excerpt structure from larchanka/swapi-mcp-example#3 (commit 858e1c1) src/tools/films.ts
SWAPI_FILMS_TS = "\n".join(
    [
        'import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";',  # 1
        'import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";',  # 2
        'import { z } from "zod";',  # 3
        'import { ApiError, type SwapiClient } from "../api-client.js";',  # 4
        'import { idFromUrl, type Film } from "../types/swapi.js";',  # 5
        'import { jsonResult, toErrorResult } from "./common.js";',  # 6
        *[f"// filler line {idx}" for idx in range(7, 93)],  # 7..92
        "    async ({ film_id: filmId, episode_id: episodeId }) => {",  # 93
        "      const hasFilmId = filmId !== undefined;",  # 94
        "      const hasEpisodeId = episodeId !== undefined;",  # 95
        "      if ((hasFilmId && hasEpisodeId) || (!hasFilmId && !hasEpisodeId)) {",  # 96
        "        throw new McpError(",  # 97
        "          ErrorCode.InvalidParams,",  # 98
        "          \"Provide exactly one of 'film_id' or 'episode_id'.\",",  # 99
        "        );",  # 100
        "      }",  # 101
        "      try {",  # 102
        "        const film =",  # 103
        "          filmId !== undefined",  # 104
        "            ? await client.getFilm(filmId)",  # 105
        "            : await requireFilmByEpisode(client, episodeId as number);",  # 106
        "        return jsonResult({",  # 107
        '          film_id: idFromUrl(film.url, "films"),',  # 108
        "          ...film,",  # 109
        "        });",  # 110
        "      } catch (error) {",  # 111
        "        return toErrorResult(error);",  # 112
        "      } finally {",  # 113
        "        cleanupFilm();",  # 114
        "      }",  # 115
        "    },",  # 116
    ]
)

SWAPI_STARSHIPS_TS = "\n".join(
    [
        'import { z } from "zod";',
        "export function registerStarshipTools(server: McpServer, client: SwapiClient) {",
        "  server.tool(",
        '    "get_starship",',
        "    {",
        "      id: z.union([z.number().int().positive(), z.string()]),",
        "    },",
        "    async ({ id }) => jsonResult(await client.getStarship(id)),",
        "  );",
        "}",
    ]
)

SWAPI_API_CLIENT_TS = "\n".join(
    [
        "export class SwapiClient {",
        "  async getResource(pathOrUrl: string): Promise<unknown> {",
        "    if (pathOrUrl.startsWith('http://') || pathOrUrl.startsWith('https://')) {",
        "      return fetch(pathOrUrl);",
        "    }",
        "    return fetch(this.baseUrl + pathOrUrl);",
        "  }",
        "  async fetchById(idOrUrl: number | string): Promise<unknown> {",
        "    return this.getResource(String(idOrUrl));",
        "  }",
        "}",
    ]
)


def _pr_meta() -> PullRequestMeta:
    return PullRequestMeta(
        title="Expand SWAPI tools",
        description=None,
        author="dev",
        source_branch="feat/tools",
        target_branch="main",
        labels=(),
        files_changed=1,
        lines_added=10,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha="858e1c1",
    )


def _full_file_block(path: str, text: str, *, block_id: str | None = None) -> ContextBlock:
    line_count = len(text.splitlines())
    return ContextBlock(
        block_id=block_id or f"l3-{path}",
        level=ContextLevel.L3,
        file_id=None,
        source=SourceRef(
            role=SourceRole.SOURCE_EXCERPT,
            repository_id=REPO_ID,
            revision="858e1c1",
            path=path,
            blob_sha="a" * 40,
            origin_paths=(),
            side=DiffSide.RIGHT,
            ranges=(LineRange(1, line_count),),
            reason="full_file",
        ),
        representation=Representation.EXACT_LINES,
        text=text,
        tokens=100,
        truncated=False,
    )


def _context(
    *,
    changed_files: tuple[ChangedFile, ...] = (),
    context_blocks: tuple[ContextBlock, ...] = (),
    snapshot: PolicySnapshot | None = None,
) -> ReviewContext:
    return ReviewContext(
        system="Review",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=_pr_meta(),
        changed_files=changed_files,
        omitted_files=(),
        policy_snapshot=snapshot,
        context_blocks=context_blocks,
    )


def _finding(
    *,
    path: str = "src/tools/films.ts",
    line: int = 96,
    title: str = "Finding",
    body: str = "Issue body",
    evidence_refs: list[EvidenceRef] | None = None,
) -> ReviewFindingV2:
    refs = evidence_refs or [
        EvidenceRef(block_id=file_block_id(path), start_line=line, end_line=line)
    ]
    return ReviewFindingV2(
        path=path,
        line=line,
        start_line=None,
        severity="high",
        category="correctness",
        title=title,
        body=body,
        suggestion=None,
        confidence=0.9,
        rule_name=None,
        evidence_refs=refs,
    )


def _candidate(candidate_id: str, finding: ReviewFindingV2) -> CandidateFindingItem:
    return CandidateFindingItem(candidate_id=candidate_id, finding=finding)


def _supported_decision(cand: CandidateFindingItem) -> VerificationDecision:
    return VerificationDecision(
        candidate_id=cand.candidate_id,
        status="supported",
        reason="Confirmed by model verifier.",
        evidence_refs=list(cand.finding.evidence_refs),
        requirement_refs=[],
    )


def _hunk_changed_file(path: str, lines: dict[int, tuple[str, str]]) -> ChangedFile:
    return ChangedFile(
        path=path,
        status="modified",
        lines=tuple(
            DiffLine(number=ln, type=kind, content=content)  # type: ignore[arg-type]
            for ln, (kind, content) in sorted(lines.items())
        ),
    )


def _snapshot() -> PolicySnapshot:
    profile = replace(
        make_complete_execution_profile("fast", review_model="model-a", verifier_model="model-b"),
        max_input_tokens=60_000,
        max_policy_tokens=12_000,
    )
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid4(),
        run_id=RUN_ID,
        repository_id=REPO_ID,
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(".review/rules.md", "# Rules\nCheck security.\n"),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=uuid4(),
        defaults_checksum="default-backend.v1",
        prompt_bindings=replace(make_complete_prompt_bindings(), review_system_version="v4"),
        execution_profile=profile,
        policy_digest="d" * 64,
    )


def test_swapi_films_modified_hunk_rejects_false_import_and_try_catch_with_l3_context() -> None:
    """Modified hunk starts at L93 while L2 holds import and L102-112 holds try/catch."""
    films_lines = SWAPI_FILMS_TS.splitlines()
    films_cf = _hunk_changed_file(
        "src/tools/films.ts",
        {ln: ("context" if ln != 94 else "added", films_lines[ln - 1]) for ln in range(93, 117)},
    )
    ctx = _context(
        changed_files=(films_cf,),
        context_blocks=(_full_file_block("src/tools/films.ts", SWAPI_FILMS_TS),),
    )

    cand_import = _candidate(
        "c-import",
        _finding(
            line=96,
            title="McpError is not imported in get_film tool",
            body=(
                "The `get_film` tool uses `McpError` and `ErrorCode` on lines 97-100, "
                "but neither is imported."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=96, end_line=100
                )
            ],
        ),
    )
    cand_try_header = _candidate(
        "c-try-header",
        _finding(
            line=102,
            title="Unhandled error in get_film tool",
            body=(
                "`get_film` calls `client.getFilm` and `requireFilmByEpisode` "
                "without a surrounding try/catch."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=102, end_line=106
                )
            ],
        ),
    )
    cand_try_inner = _candidate(
        "c-try-inner",
        _finding(
            line=105,
            title="No try/catch around API call",
            body="client.getFilm is called without a try/catch block.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=105, end_line=105
                )
            ],
        ),
    )

    candidates = (cand_import, cand_try_header, cand_try_inner)
    decisions = tuple(_supported_decision(c) for c in candidates)
    supported, rejected = filter_verified_candidates(
        candidates=candidates,
        decisions=decisions,
        context=ctx,
        claim_syntax=INSPECTOR,
    )

    assert supported == []
    assert [c.candidate_id for c, _, _ in rejected] == ["c-import", "c-try-header", "c-try-inner"]
    assert all(status == "contradicted" for _, status, _ in rejected)


def test_try_catch_check_excludes_catch_and_finally_clauses_and_try_finally() -> None:
    """Only caught try bodies count; catch/finally and uncaught try...finally stay supported."""
    films_cf = _hunk_changed_file(
        "src/tools/films.ts", {112: ("added", "return toErrorResult(error);")}
    )
    try_finally_ts = (
        "async function f() {\n  try {\n    await work();\n  } finally {\n    done();\n  }\n}\n"
    )
    tf_cf = ChangedFile(
        path="src/tools/tf.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(try_finally_ts.splitlines(), 1)
        ),
    )
    ctx = _context(
        changed_files=(films_cf, tf_cf),
        context_blocks=(_full_file_block("src/tools/films.ts", SWAPI_FILMS_TS),),
    )

    in_catch = _candidate(
        "c-in-catch",
        _finding(line=112, title="Unhandled error", body="Called without a try/catch block."),
    )
    in_finally = _candidate(
        "c-in-finally",
        _finding(line=114, title="Unhandled cleanup", body="cleanupFilm lacks a try/catch."),
    )
    in_try_finally = _candidate(
        "c-try-finally",
        _finding(
            path="src/tools/tf.ts",
            line=3,
            title="Uncaught work()",
            body="work() runs without a try/catch handler.",
        ),
    )
    supported, rejected = filter_verified_candidates(
        candidates=(in_catch, in_finally, in_try_finally),
        decisions=(
            _supported_decision(in_catch),
            _supported_decision(in_finally),
            _supported_decision(in_try_finally),
        ),
        context=ctx,
        claim_syntax=INSPECTOR,
    )

    assert rejected == []
    assert [c.candidate_id for c in supported] == ["c-in-catch", "c-in-finally", "c-try-finally"]


def test_ts_aliased_imports_and_module_path_complaints() -> None:
    """Only local bindings count; remediation and wrong-source complaints are not suppressed."""
    aliased_ts = (
        b'import { ErrorCode, McpError as McpErr } from "@modelcontextprotocol/sdk/types.js";\n'
        b"export function check() {\n"
        b"  throw new McpErr(ErrorCode.InternalError, 'boom');\n"
        b"}\n"
    )

    # Claim about the alias 'McpErr' is contradicted; claim about unaliased 'McpError' is not.
    alias_false = _finding(
        path="src/a.ts",
        line=3,
        title="McpErr is not imported",
        body="`McpErr` is used without importing.",
    )
    orig_missing = _finding(
        path="src/a.ts",
        line=3,
        title="McpError is not imported",
        body="McpError is aliased elsewhere.",
    )
    remediation_only = _finding(
        path="src/a.ts",
        line=3,
        title="Wrong import subpath",
        body="Import ErrorCode from '@modelcontextprotocol/sdk/server/mcp.js' instead.",
    )
    wrong_source = _finding(
        path="src/a.ts",
        line=3,
        title="Wrong import module",
        body="ErrorCode is not imported from '@modelcontextprotocol/sdk/server/mcp.js'.",
    )

    assert (
        audit_candidate_claim(alias_false, aliased_ts, claim_syntax=INSPECTOR).status
        == "contradicted"
    )
    assert (
        audit_candidate_claim(orig_missing, aliased_ts, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(remediation_only, aliased_ts, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(wrong_source, aliased_ts, claim_syntax=INSPECTOR).status
        == "supported"
    )


def test_try_catch_stops_at_nested_function_boundaries_for_ts_and_python() -> None:
    """Enclosing try/catch checks stop at nested function/lambda/callback boundaries."""
    ts_source = (
        b"try {\n"
        b"  items.forEach(async (x) => {\n"
        b"    await f(x);\n"
        b"  });\n"
        b"  function inner() {\n"
        b"    boom();\n"
        b"  }\n"
        b"  syncWork();\n"
        b"} catch (err) {\n"
        b"  console.error(err);\n"
        b"}\n"
    )
    # Inside callback / inner function: outer try does not catch runtime throws
    f_cb = _finding(
        path="src/a.ts",
        line=3,
        title="Uncaught async call",
        body="await f(x) lacks a try/catch handler.",
    )
    f_inner = _finding(
        path="src/a.ts",
        line=6,
        title="Uncaught boom",
        body="boom() runs without a try/catch.",
    )
    # Direct try statement body child: enclosed
    f_sync = _finding(
        path="src/a.ts",
        line=8,
        title="Uncaught syncWork",
        body="syncWork() runs without a try/catch.",
    )

    assert audit_candidate_claim(f_cb, ts_source, claim_syntax=INSPECTOR).status == "supported"
    assert audit_candidate_claim(f_inner, ts_source, claim_syntax=INSPECTOR).status == "supported"
    assert audit_candidate_claim(f_sync, ts_source, claim_syntax=INSPECTOR).status == "contradicted"

    py_source = (
        b"try:\n"
        b"    def inner():\n"
        b"        boom()\n"
        b"    fn = lambda x: bad(x)\n"
        b"    sync_call()\n"
        b"except Exception:\n"
        b"    pass\n"
    )
    f_py_inner = _finding(
        path="app/a.py",
        line=3,
        title="Uncaught boom",
        body="boom() is not wrapped in a try/except.",
    )
    f_py_lambda = _finding(
        path="app/a.py",
        line=4,
        title="Uncaught bad",
        body="bad(x) runs without a surrounding try/except.",
    )
    f_py_sync = _finding(
        path="app/a.py",
        line=5,
        title="Uncaught sync_call",
        body="sync_call() runs without a surrounding try/except.",
    )

    assert audit_candidate_claim(f_py_inner, py_source).status == "supported"
    assert audit_candidate_claim(f_py_lambda, py_source).status == "supported"
    assert audit_candidate_claim(f_py_sync, py_source).status == "contradicted"


def test_import_type_and_type_checking_are_not_treated_as_runtime_value_imports() -> None:
    """Type-only imports and TYPE_CHECKING/local in Python do not contradict missing claims."""
    ts_source = (
        b"import type { TypeOnly } from './types';\n"
        b"import { type InnerType, RealVal } from './vals';\n"
        b"export function run(): void {}\n"
    )
    f_type_only = _finding(
        path="src/a.ts",
        line=3,
        title="TypeOnly is not imported",
        body="`TypeOnly` is not imported.",
    )
    f_inner_type = _finding(
        path="src/a.ts",
        line=3,
        title="InnerType is not imported",
        body="`InnerType` is not imported.",
    )
    f_real_val = _finding(
        path="src/a.ts",
        line=3,
        title="RealVal is not imported",
        body="`RealVal` is not imported.",
    )

    assert (
        audit_candidate_claim(f_type_only, ts_source, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(f_inner_type, ts_source, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(f_real_val, ts_source, claim_syntax=INSPECTOR).status
        == "contradicted"
    )

    py_source = (
        b"from typing import TYPE_CHECKING\n"
        b"if TYPE_CHECKING:\n"
        b"    import TypeOnlyMod\n"
        b"    from typing_ext import TypeOnlySym\n"
        b"def helper():\n"
        b"    import local_mod\n"
        b"import runtime_val\n"
    )
    f_py_mod = _finding(
        path="app/a.py",
        line=7,
        title="TypeOnlyMod is not imported",
        body="`TypeOnlyMod` is not imported.",
    )
    f_py_sym = _finding(
        path="app/a.py",
        line=7,
        title="TypeOnlySym is not imported",
        body="`TypeOnlySym` is not imported.",
    )
    f_py_local = _finding(
        path="app/a.py",
        line=7,
        title="local_mod is not imported",
        body="`local_mod` is not imported.",
    )
    f_py_val = _finding(
        path="app/a.py",
        line=7,
        title="runtime_val is not imported",
        body="`runtime_val` is not imported.",
    )

    assert audit_candidate_claim(f_py_mod, py_source).status == "supported"
    assert audit_candidate_claim(f_py_sym, py_source).status == "supported"
    assert audit_candidate_claim(f_py_local, py_source).status == "supported"
    assert audit_candidate_claim(f_py_val, py_source).status == "contradicted"


def test_qualified_not_imported_claims_are_not_contradicted() -> None:
    """Claims qualified with 'as a value', 'at runtime', or other files are not contradicted."""
    ts_source = (
        b"import { RealVal } from './vals';\n"
        b"export function run() {}\n"
    )
    f_as_value = _finding(
        path="src/a.ts",
        line=2,
        title="RealVal is not imported as a value",
        body="`RealVal` is not imported as a value.",
    )
    f_at_runtime = _finding(
        path="src/a.ts",
        line=2,
        title="RealVal is not imported at runtime",
        body="`RealVal` is not imported at runtime.",
    )
    f_other_file = _finding(
        path="src/a.ts",
        line=2,
        title="RealVal in other files",
        body="`RealVal` is not imported in handler.ts.",
    )
    f_unconditional = _finding(
        path="src/a.ts",
        line=2,
        title="RealVal is not imported",
        body="`RealVal` is not imported.",
    )

    assert (
        audit_candidate_claim(f_as_value, ts_source, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(f_at_runtime, ts_source, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(f_other_file, ts_source, claim_syntax=INSPECTOR).status
        == "supported"
    )
    assert (
        audit_candidate_claim(f_unconditional, ts_source, claim_syntax=INSPECTOR).status
        == "contradicted"
    )


def test_swapi_comparative_id_or_url_checks_cited_external_block_contents() -> None:
    """Comparative claims must cite an external block whose referenced slice contains the param."""
    starships_cf = ChangedFile(
        path="src/tools/starships.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_STARSHIPS_TS.splitlines(), 1)
        ),
    )
    client_cf = ChangedFile(
        path="src/api-client.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_API_CLIENT_TS.splitlines(), 1)
        ),
    )
    ctx = _context(changed_files=(starships_cf, client_cf))

    no_external_ref = _candidate(
        "c-no-ext",
        _finding(
            path="src/tools/starships.ts",
            line=6,
            title="Inconsistent parameter naming in get_starship",
            body=(
                "The tool uses `id` as the parameter name, "
                "while other tools in the codebase use `idOrUrl`."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=6, end_line=6
                )
            ],
        ),
    )
    unrelated_external_lines = _candidate(
        "c-bad-ref",
        _finding(
            path="src/tools/starships.ts",
            line=6,
            title="Inconsistent parameter naming in get_starship",
            body="The tool uses `id`, while other tools in the codebase use `idOrUrl`.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=6, end_line=6
                ),
                # Lines 1-2 of api-client.ts do NOT contain idOrUrl!
                EvidenceRef(block_id=file_block_id("src/api-client.ts"), start_line=1, end_line=2),
            ],
        ),
    )
    valid_external_ref = _candidate(
        "c-good-ref",
        _finding(
            path="src/tools/starships.ts",
            line=6,
            title="Inconsistent parameter naming",
            body="Other files use `idOrUrl`.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=6, end_line=6
                ),
                EvidenceRef(block_id=file_block_id("src/api-client.ts"), start_line=8, end_line=9),
            ],
        ),
    )
    prose_stopword_finding = _candidate(
        "c-prose",
        _finding(
            path="src/tools/starships.ts",
            line=8,
            title="Missing request timeout",
            body="Other handlers use explicit timeouts rather than bare fetch calls.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=8, end_line=8
                )
            ],
        ),
    )

    candidates = (
        no_external_ref,
        unrelated_external_lines,
        valid_external_ref,
        prose_stopword_finding,
    )
    supported, rejected = filter_verified_candidates(
        candidates=candidates,
        decisions=tuple(_supported_decision(c) for c in candidates),
        context=ctx,
        claim_syntax=INSPECTOR,
    )

    assert [c.candidate_id for c in supported] == ["c-good-ref", "c-prose"]
    assert [(c.candidate_id, st) for c, st, _ in rejected] == [
        ("c-no-ext", "insufficient_context"),
        ("c-bad-ref", "insufficient_context"),
    ]


def test_python_jsx_go_and_bounds_handling(caplog: pytest.LogCaptureFixture) -> None:
    """Verify .py/.pyi AST checks, SyntaxError logging, .jsx support, .go skip, and bounds."""
    py_source = (
        b"from pkg import Client as ApiClient\n"
        b"def run():\n"
        b"    try:\n"
        b"        call_api()\n"
        b"    except ValueError:\n"
        b"        recover()\n"
        b"    finally:\n"
        b"        cleanup()\n"
    )

    assert (
        audit_candidate_claim(
            _finding(
                path="app/svc.pyi",
                line=4,
                title="Missing import for ApiClient",
                body="Missing import.",
            ),
            py_source,
        ).status
        == "contradicted"
    )
    assert (
        audit_candidate_claim(
            _finding(
                path="app/svc.py", line=4, title="No try/except around call_api", body="No handler."
            ),
            py_source,
        ).status
        == "contradicted"
    )
    # catch and finally lines must not be marked as inside try body
    assert (
        audit_candidate_claim(
            _finding(
                path="app/svc.py", line=6, title="No try/except in recover", body="Uncaught error."
            ),
            py_source,
        ).status
        == "supported"
    )
    assert (
        audit_candidate_claim(
            _finding(
                path="app/svc.py", line=8, title="No try/except in cleanup", body="Uncaught error."
            ),
            py_source,
        ).status
        == "supported"
    )

    jsx_src = b"import { Widget } from './w';\nexport const App = () => <Widget />;\n"
    assert (
        audit_candidate_claim(
            _finding(
                path="src/App.jsx", line=2, title="Widget is not imported", body="Missing Widget."
            ),
            jsx_src,
            claim_syntax=INSPECTOR,
        ).status
        == "contradicted"
    )

    go_src = b"package main\nfunc f() {}\n"
    assert (
        audit_candidate_claim(
            _finding(path="main.go", line=2, title="fmt is not imported", body="Missing fmt."),
            go_src,
            claim_syntax=INSPECTOR,
        ).status
        == "supported"
    )

    caplog.set_level(logging.WARNING)
    bad_py = b"def broken(:\n"
    assert (
        audit_candidate_claim(
            _finding(path="app/bad.py", line=1, title="Foo is not imported", body="Missing Foo."),
            bad_py,
        ).status
        == "supported"
    )
    assert any("Python AST parse failed" in rec.message for rec in caplog.records)

    oversized = b"x = 1\n" + b"a" * MAX_SOURCE_BYTES
    assert (
        audit_candidate_claim(
            _finding(path="src/big.ts", line=1, title="Foo is not imported", body="Missing Foo."),
            oversized,
            claim_syntax=INSPECTOR,
        ).status
        == "supported"
    )
    assert any("exceeds" in rec.message for rec in caplog.records)


def test_iterative_try_walk_handles_deep_nesting_and_modified_without_full_file() -> None:
    """Deeply nested expressions do not hit RecursionError; sparse modified hunks fail open."""
    deep_expr = "(" * 2500 + "work()" + ")" * 2500
    deep_ts = (
        f"function run() {{\n  try {{\n    {deep_expr};\n  }} catch (e) {{\n    log(e);\n  }}\n}}\n"
    )
    assert (
        audit_candidate_claim(
            _finding(path="src/deep.ts", line=3, title="Missing try/catch", body="No try/catch."),
            deep_ts.encode("utf-8"),
            claim_syntax=INSPECTOR,
        ).status
        == "contradicted"
    )

    # Sparse modified hunk (starts at L93) without L3 full_file: never fabricate fake L1 source!
    sparse_cf = _hunk_changed_file("src/tools/films.ts", {93: ("added", "const x = 1;")})
    sparse_ctx = _context(changed_files=(sparse_cf,))
    assert extract_full_source_bytes(sparse_ctx, "src/tools/films.ts") is None

    # Even without L3 full_file, comparative check still runs on the sparse context.
    res = audit_candidate_claim(
        _finding(
            path="src/tools/films.ts",
            line=93,
            title="Parameter mismatch",
            body="Other tools in the codebase use `idOrUrl`.",
        ),
        extract_full_source_bytes(sparse_ctx, "src/tools/films.ts"),
        claim_syntax=INSPECTOR,
        context_blocks=collect_context_blocks(sparse_ctx),
    )
    assert res == ClaimAuditResult(
        status="insufficient_context",
        reason="Claim asserts other tools/files use 'idOrUrl' without external block evidence",
    )


def test_ac3_true_findings_and_out_of_scope_logic_remain_supported() -> None:
    """Authentic SWAPI security/coverage findings and out-of-scope logical claims pass audit."""
    ctx = _context(
        changed_files=(
            ChangedFile(
                path="src/api-client.ts",
                status="added",
                lines=tuple(
                    DiffLine(i, "added", line)
                    for i, line in enumerate(SWAPI_API_CLIENT_TS.splitlines(), 1)
                ),
            ),
            ChangedFile(
                path="src/tools/films.ts",
                status="added",
                lines=tuple(
                    DiffLine(i, "added", line)
                    for i, line in enumerate(SWAPI_FILMS_TS.splitlines(), 1)
                ),
            ),
        )
    )
    ssrf = _candidate(
        "c-ssrf",
        _finding(
            path="src/api-client.ts",
            line=4,
            title="SSRF vulnerability in getResource",
            body=(
                "getResource forwards unvalidated URLs to fetch "
                "without checking origin against baseUrl."
            ),
            evidence_refs=[
                EvidenceRef(block_id=file_block_id("src/api-client.ts"), start_line=3, end_line=4)
            ],
        ),
    )
    id_from_url = _candidate(
        "c-id-url",
        _finding(
            path="src/api-client.ts",
            line=9,
            title="idFromUrl does not enforce resource type",
            body=(
                "Extracting numeric id without matching the expected resource path segment "
                "misroutes URLs."
            ),
            evidence_refs=[
                EvidenceRef(block_id=file_block_id("src/api-client.ts"), start_line=8, end_line=9)
            ],
        ),
    )
    # Scope boundary (#445): 'exactly one' semantic claim is outside deterministic AST audit.
    exactly_one = _candidate(
        "c-logic",
        _finding(
            path="src/tools/films.ts",
            line=96,
            title="Missing validation for exactly one identifier",
            body="The check does not enforce that exactly one of film_id or episode_id is passed.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=94, end_line=96
                )
            ],
        ),
    )

    supported, rejected = filter_verified_candidates(
        candidates=(ssrf, id_from_url, exactly_one),
        decisions=(
            _supported_decision(ssrf),
            _supported_decision(id_from_url),
            _supported_decision(exactly_one),
        ),
        context=ctx,
        claim_syntax=INSPECTOR,
    )
    assert rejected == []
    assert [c.candidate_id for c in supported] == ["c-ssrf", "c-id-url", "c-logic"]


class _PipelineStub:
    def __init__(
        self,
        files: tuple[ChangedFile, ...],
        snapshot: PolicySnapshot,
        payload: ReviewContextPayload,
    ) -> None:
        self.files = files
        self.snapshot = snapshot
        self.payload = payload
        self.published: list[Any] = []
        self.excluded = lambda _path: False  # noqa: E731
        self.diff_old_sha: str | None = None
        self.head_sha = "858e1c1"
        self.snapshot_for_llm = snapshot
        self.ignored_file_paths: frozenset[str] = frozenset()

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return GeneratedConventions(
            CachedConventions(UUID(int=1), None, run_id, (), (), {}), None, True
        )

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=self.files,
            omitted_files=(),
            policy_snapshot=self.snapshot,
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _pr_meta()

    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        self.published.append(raw_output)
        return True


class _DraftReviewStub:
    def __init__(self, draft: dict[str, Any], meta: PullRequestMeta) -> None:
        self._draft = draft
        self._meta = meta

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return self._meta

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        return self._draft


class _RubberStampVerifierStub:
    """Approves every candidate to exercise deterministic claim auditing downstream."""

    def __init__(self, block_by_path: dict[str, str]) -> None:
        self._block_by_path = block_by_path

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> dict[str, Any]:
        decisions = []
        for cid, path, line in re.findall(
            r'candidate_id="([^"]+)"[^>]*path="([^"]+)"[^>]*line="(\d+)"',
            user_prompt,
        ):
            decisions.append(
                {
                    "candidate_id": cid,
                    "status": "supported",
                    "reason": "Verified from context.",
                    "evidence_refs": [
                        {
                            "block_id": self._block_by_path[path],
                            "start_line": int(line),
                            "end_line": int(line),
                        }
                    ],
                    "requirement_refs": [],
                }
            )
        return {"decisions": decisions}


def test_execute_review_run_drops_false_claims_while_retaining_valid_findings() -> None:
    """ExecuteReviewRun with TreeSitterClaimSyntaxInspector rejects SWAPI hallucinations."""
    films_lines = SWAPI_FILMS_TS.splitlines()
    films_cf = _hunk_changed_file(
        "src/tools/films.ts",
        {ln: ("context" if ln != 94 else "added", films_lines[ln - 1]) for ln in range(93, 117)},
    )
    starships_cf = ChangedFile(
        path="src/tools/starships.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_STARSHIPS_TS.splitlines(), 1)
        ),
    )
    client_cf = ChangedFile(
        path="src/api-client.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_API_CLIENT_TS.splitlines(), 1)
        ),
    )
    snapshot = _snapshot()
    payload = ReviewContextPayload(
        status=ContextStatus.REVIEW,
        reason=None,
        policy_snapshot_id=snapshot.snapshot_id,
        policy_digest=snapshot.policy_digest,
        diff_digest="d" * 64,
        blocks=(_full_file_block("src/tools/films.ts", SWAPI_FILMS_TS),),
        omissions=(),
        imports=(),
        levels_present=(ContextLevel.L3,),
        reviewable_lines=40,
        ignored_files=0,
        tokens_used=200,
    )
    pipeline = _PipelineStub((films_cf, starships_cf, client_cf), snapshot, payload)
    draft = {
        "findings": [
            {
                "path": "src/tools/films.ts",
                "line": 96,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "McpError is not imported in get_film tool",
                "body": "McpError is not imported before use.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
                "evidence_refs": [
                    {
                        "block_id": file_block_id("src/tools/films.ts"),
                        "start_line": 96,
                        "end_line": 96,
                    }
                ],
            },
            {
                "path": "src/tools/films.ts",
                "line": 105,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Unhandled error in get_film",
                "body": "Calls client.getFilm without a surrounding try/catch.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
                "evidence_refs": [
                    {
                        "block_id": file_block_id("src/tools/films.ts"),
                        "start_line": 105,
                        "end_line": 105,
                    }
                ],
            },
            {
                "path": "src/tools/starships.ts",
                "line": 6,
                "start_line": None,
                "severity": "low",
                "category": "readability",
                "title": "Inconsistent id parameter",
                "body": "Other tools in the codebase use `idOrUrl`.",
                "suggestion": None,
                "confidence": 0.8,
                "rule_name": None,
                "evidence_refs": [
                    {
                        "block_id": file_block_id("src/tools/starships.ts"),
                        "start_line": 6,
                        "end_line": 6,
                    }
                ],
            },
            {
                "path": "src/api-client.ts",
                "line": 4,
                "start_line": None,
                "severity": "critical",
                "category": "security",
                "title": "SSRF vulnerability in getResource",
                "body": "Arbitrary HTTP URLs are passed directly to fetch.",
                "suggestion": None,
                "confidence": 0.95,
                "rule_name": None,
                "evidence_refs": [
                    {"block_id": file_block_id("src/api-client.ts"), "start_line": 4, "end_line": 4}
                ],
            },
        ],
        "summary": {
            "problem": "Issues found.",
            "done_well": "Typed schemas.",
            "effort": "moderate",
        },
    }
    verifier = ReflectionPass(
        _RubberStampVerifierStub(
            {
                "src/tools/films.ts": file_block_id("src/tools/films.ts"),
                "src/tools/starships.ts": file_block_id("src/tools/starships.ts"),
                "src/api-client.ts": file_block_id("src/api-client.ts"),
            }
        )
    )
    run = ExecuteReviewRun(
        pipeline,
        pipeline,
        _DraftReviewStub(draft, _pr_meta()),
        pipeline,
        context_filter=pipeline,
        reflection_pass=verifier,
        claim_syntax=INSPECTOR,
    )

    assert asyncio.run(run.execute(RUN_ID)) is True
    (output,) = pipeline.published
    assert isinstance(output, VerifiedReviewOutput)
    findings = cast(list[dict[str, Any]], output["findings"])
    assert [f["title"] for f in findings] == ["SSRF vulnerability in getResource"]

    # AC4 guard: without TS syntax inspector, the AST-based false claims leak through.
    pipeline_no_ast = _PipelineStub((films_cf, starships_cf, client_cf), snapshot, payload)
    run_no_ast = ExecuteReviewRun(
        pipeline_no_ast,
        pipeline_no_ast,
        _DraftReviewStub(draft, _pr_meta()),
        pipeline_no_ast,
        context_filter=pipeline_no_ast,
        reflection_pass=verifier,
        claim_syntax=None,
    )
    assert asyncio.run(run_no_ast.execute(RUN_ID)) is True
    (output_no_ast,) = pipeline_no_ast.published
    assert isinstance(output_no_ast, VerifiedReviewOutput)
    no_ast_findings = cast(list[dict[str, Any]], output_no_ast["findings"])
    assert len(no_ast_findings) == 3

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
    ContextBlockSnippet,
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
from tests.execute_review_fakes import _AcceptingCheckpointPublisher
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

REPO_ID = UUID("00000000-0000-0000-0000-000000004451")
RUN_ID = UUID("00000000-0000-0000-0000-000000004452")
INSPECTOR = TreeSitterClaimSyntaxInspector()

# Synthetic excerpt structure based on larchanka/swapi-mcp-example#3
# (commit 8bc8b5d) src/tools/films.ts
SWAPI_FILMS_TS = "\n".join(
    [
        'import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";',
        'import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";',
        'import { z } from "zod";',
        'import { ApiError, type SwapiClient } from "../api-client.js";',
        'import { idFromUrl, type Film } from "../types/swapi.js";',
        'import { jsonResult, toErrorResult } from "./common.js";',
        "",
        "interface FilmSummary {",
        "  film_id: number | undefined;",
        "  episode_id: number;",
        "  title: string;",
        "  release_date: string;",
        "  director: string;",
        "  producer: string;",
        "  character_count: number;",
        "}",
        "",
        "interface CharacterSummary {",
        "  id: number | undefined;",
        "  name: string;",
        "  gender: string;",
        "  birth_year: string;",
        "  height: string;",
        "  mass: string;",
        "  hair_color: string;",
        "  eye_color: string;",
        "  url: string;",
        "}",
        "",
        "interface UnavailableCharacter {",
        "  id: number | undefined;",
        "  url: string;",
        "  error: string;",
        "}",
        "",
        "export function registerFilmTools(server: McpServer, client: SwapiClient): void {",
        "  server.registerTool(",
        '    "list_films",',
        "    {",
        '      title: "List Star Wars episodes",',
        "      description:",
        (
            '        "List all Star Wars film episodes available in SWAPI, '
            'sorted by episode number. " +'
        ),
        (
            '        "Returns for each episode: film_id (SWAPI resource id), '
            'episode_id (episode number), " +'
        ),
        (
            '        "title, release date, director, producer and '
            'how many characters appear in it. " +'
        ),
        ('        "Use this tool first to discover episodes; then pass film_id or episode_id " +'),
        (
            '        "to get_film_characters to fetch characters, '
            'or get_film for full episode details.",'
        ),
        "      inputSchema: {},",
        "    },",
        "    async () => {",
        "      try {",
        "        const films = await client.listFilms();",
        "        const episodes = films",
        "          .map(toFilmSummary)",
        "          .sort((a, b) => a.episode_id - b.episode_id);",
        "        return jsonResult({ total_episodes: episodes.length, episodes });",
        "      } catch (error) {",
        "        return toErrorResult(error);",
        "      }",
        "    },",
        "  );",
        "",
        "  server.registerTool(",
        '    "get_film",',
        "    {",
        '      title: "Get Star Wars film details",',
        "      description:",
        (
            '        "Get detailed information about a specific Star Wars film episode, '
            'including title, " +'
        ),
        (
            '        "episode number, opening crawl text, director, producer, release date, '
            'and links to " +'
        ),
        '        "characters, planets, starships, vehicles, and species. " +',
        (
            '        "Provide EITHER film_id (SWAPI film resource id, 1-6 — '
            'see the film_id field of list_films) " +'
        ),
        (
            "        \"OR episode_id (the episode number, 1-6, e.g. 4 for 'A New Hope'); "
            'exactly one of them is required. " +'
        ),
        ('        "Note: SWAPI film ids do not match episode numbers (film_id 1 is Episode IV).",'),
        "      inputSchema: {",
        "        film_id: z",
        "          .number()",
        "          .int()",
        "          .positive()",
        "          .optional()",
        "          .describe(",
        '            "SWAPI film resource id (1-6). Film 1 is Episode IV, film 4 is Episode I. " +',
        '              "Use the film_id returned by list_films.",',
        "          ),",
        "        episode_id: z",
        "          .number()",
        "          .int()",
        "          .positive()",
        "          .optional()",
        "          .describe(",
        (
            "            \"Episode number (1-6), e.g. 4 for 'A New Hope'. "
            'Provide this OR film_id, not both.",'
        ),
        "          ),",
        "      },",
        "    },",
        "    async ({ film_id: filmId, episode_id: episodeId }) => {",
        "      if ((filmId === undefined) === (episodeId === undefined)) {",
        "        throw new McpError(",
        "          ErrorCode.InvalidParams,",
        (
            "          \"Provide exactly one of 'film_id' or 'episode_id' "
            'to identify the episode.",'
        ),
        "        );",
        "      }",
        "      try {",
        "        const film =",
        "          filmId !== undefined",
        "            ? await client.getFilm(filmId)",
        "            : await requireFilmByEpisode(client, episodeId as number);",
        "",
        "        return jsonResult({",
        '          film_id: idFromUrl(film.url, "films"),',
        "          ...film,",
        "        });",
        "      } catch (error) {",
        "        return toErrorResult(error);",
        "      }",
        "    },",
        "  );",
        "}",
    ]
)

# Synthetic fixture for comparative tool citation tests (not authentic SWAPI source)
SWAPI_VEHICLES_TS = "\n".join(
    [
        'import { z } from "zod";',
        "export function registerVehicleTools(server: McpServer, client: SwapiClient) {",
        "  server.tool(",
        '    "get_vehicle",',
        "    {",
        "      idOrUrl: z.union([z.number().int().positive(), z.string()]),",
        "    },",
        "    async ({ idOrUrl }) => jsonResult(await client.getVehicle(idOrUrl)),",
        "  );",
        "}",
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
    """Modified hunk starts at L93 while L2 holds import and L100-112 holds try/catch."""
    films_lines = SWAPI_FILMS_TS.splitlines()
    films_cf = _hunk_changed_file(
        "src/tools/films.ts",
        {ln: ("context" if ln != 94 else "added", films_lines[ln - 1]) for ln in range(93, 115)},
    )
    ctx = _context(
        changed_files=(films_cf,),
        context_blocks=(_full_file_block("src/tools/films.ts", SWAPI_FILMS_TS),),
    )

    cand_import = _candidate(
        "c-import",
        _finding(
            line=95,
            title="McpError is not imported in get_film tool",
            body=(
                "The `get_film` tool uses `McpError` and `ErrorCode` on lines 95-98, "
                "but neither is imported."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=94, end_line=98
                )
            ],
        ),
    )
    cand_try_header = _candidate(
        "c-try-header",
        _finding(
            line=100,
            title="Unhandled error in get_film tool",
            body=(
                "`get_film` calls `client.getFilm` and `requireFilmByEpisode` "
                "without a surrounding try/catch."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=100, end_line=105
                )
            ],
        ),
    )
    cand_try_inner = _candidate(
        "c-try-inner",
        _finding(
            line=103,
            title="No try/catch around API call",
            body="client.getFilm is called without a try/catch block.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=103, end_line=103
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


@pytest.mark.parametrize(
    ("path", "line", "title", "body"),
    [
        ("src/tools/films.ts", 111, "Unhandled error", "Called without a try/catch block."),
        ("src/tools/tf.ts", 5, "Unhandled cleanup", "done() lacks a try/catch."),
        ("src/tools/tf.ts", 3, "Uncaught work()", "work() runs without a try/catch handler."),
    ],
)
def test_try_catch_check_excludes_catch_and_finally_clauses_and_try_finally(
    path: str, line: int, title: str, body: str
) -> None:
    """Only caught try bodies count; catch/finally and uncaught try...finally stay supported."""
    films_cf = _hunk_changed_file(
        "src/tools/films.ts", {111: ("added", "return toErrorResult(error);")}
    )
    try_finally_ts = (
        "async function f() {\n  try {\n    await work();\n  } finally {\n    done();\n  }\n}\n"
    )
    tf_cf = ChangedFile(
        path="src/tools/tf.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", ln) for i, ln in enumerate(try_finally_ts.splitlines(), 1)
        ),
    )
    ctx = _context(
        changed_files=(films_cf, tf_cf),
        context_blocks=(_full_file_block("src/tools/films.ts", SWAPI_FILMS_TS),),
    )

    cand = _candidate("c-test", _finding(path=path, line=line, title=title, body=body))
    supported, rejected = filter_verified_candidates(
        candidates=(cand,),
        decisions=(_supported_decision(cand),),
        context=ctx,
        claim_syntax=INSPECTOR,
    )

    assert rejected == []
    assert [c.candidate_id for c in supported] == ["c-test"]


@pytest.mark.parametrize(
    ("title", "body", "expected_status"),
    [
        ("McpErr is not imported", "`McpErr` is used without importing.", "contradicted"),
        ("McpError is not imported", "McpError is aliased elsewhere.", "supported"),
        (
            "Wrong import subpath",
            "Import ErrorCode from '@modelcontextprotocol/sdk/server/mcp.js' instead.",
            "supported",
        ),
        (
            "Wrong import module",
            "ErrorCode is not imported from '@modelcontextprotocol/sdk/server/mcp.js'.",
            "supported",
        ),
    ],
)
def test_ts_aliased_imports_and_module_path_complaints(
    title: str, body: str, expected_status: str
) -> None:
    """Only local bindings count; remediation and wrong-source complaints are not suppressed."""
    aliased_ts = (
        b'import { ErrorCode, McpError as McpErr } from "@modelcontextprotocol/sdk/types.js";\n'
        b"export function check() {\n"
        b"  throw new McpErr(ErrorCode.InternalError, 'boom');\n"
        b"}\n"
    )
    finding = _finding(path="src/a.ts", line=3, title=title, body=body)
    assert (
        audit_candidate_claim(finding, aliased_ts, claim_syntax=INSPECTOR).status == expected_status
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
        b"    fn = lambda x: (\n"
        b"        bad(x)\n"
        b"    )\n"
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
        line=5,
        title="Uncaught bad",
        body="bad(x) runs without a surrounding try/except.",
    )
    f_py_sync = _finding(
        path="app/a.py",
        line=7,
        title="Uncaught sync_call",
        body="sync_call() runs without a surrounding try/except.",
    )

    assert audit_candidate_claim(f_py_inner, py_source).status == "supported"
    assert audit_candidate_claim(f_py_lambda, py_source).status == "supported"
    assert audit_candidate_claim(f_py_sync, py_source).status == "contradicted"


@pytest.mark.parametrize(
    ("symbol", "expected_status"),
    [
        ("TypeOnly", "supported"),
        ("InnerType", "supported"),
        ("RealVal", "contradicted"),
    ],
)
def test_ts_import_type_is_not_treated_as_runtime_value_import(
    symbol: str, expected_status: str
) -> None:
    """TypeScript type-only imports do not count as runtime value bindings."""
    ts_source = (
        b"import type { TypeOnly } from './types';\n"
        b"import { type InnerType, RealVal } from './vals';\n"
        b"export function run(): void {}\n"
    )
    finding = _finding(
        path="src/a.ts",
        line=3,
        title=f"{symbol} is not imported",
        body=f"`{symbol}` is not imported.",
    )
    assert (
        audit_candidate_claim(finding, ts_source, claim_syntax=INSPECTOR).status == expected_status
    )


@pytest.mark.parametrize(
    ("symbol", "expected_status"),
    [
        ("TypeOnlyMod", "supported"),
        ("TypeOnlySym", "supported"),
        ("local_mod", "supported"),
        ("runtime_val", "contradicted"),
    ],
)
def test_python_type_checking_and_local_imports_are_not_runtime_imports(
    symbol: str, expected_status: str
) -> None:
    """Python TYPE_CHECKING and local imports do not count as module runtime value bindings."""
    py_source = (
        b"from typing import TYPE_CHECKING\n"
        b"if TYPE_CHECKING:\n"
        b"    import TypeOnlyMod\n"
        b"    from typing_ext import TypeOnlySym\n"
        b"def helper():\n"
        b"    import local_mod\n"
        b"import runtime_val\n"
    )
    finding = _finding(
        path="app/a.py",
        line=7,
        title=f"{symbol} is not imported",
        body=f"`{symbol}` is not imported.",
    )
    assert audit_candidate_claim(finding, py_source).status == expected_status


@pytest.mark.parametrize(
    ("title", "body", "expected_status"),
    [
        (
            "RealVal is not imported as a value",
            "`RealVal` is not imported as a value.",
            "supported",
        ),
        (
            "RealVal is not imported at runtime",
            "`RealVal` is not imported at runtime.",
            "supported",
        ),
        (
            "RealVal in other files",
            "`RealVal` is not imported in handler.ts.",
            "supported",
        ),
        (
            "RealVal in test file",
            "RealVal is not imported in the test file.",
            "supported",
        ),
        (
            "RealVal in other dir",
            "RealVal is not imported in src/tools/people.",
            "supported",
        ),
        (
            "RealVal is not imported",
            "`RealVal` is not imported.",
            "contradicted",
        ),
    ],
)
def test_qualified_not_imported_claims_are_not_contradicted(
    title: str, body: str, expected_status: str
) -> None:
    """Claims qualified with 'as a value', 'at runtime', or other files are not contradicted."""
    ts_source = b"import { RealVal } from './vals';\nexport function run() {}\n"
    finding = _finding(path="src/a.ts", line=2, title=title, body=body)
    assert (
        audit_candidate_claim(finding, ts_source, claim_syntax=INSPECTOR).status == expected_status
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
    vehicles_cf = ChangedFile(
        path="src/tools/vehicles.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_VEHICLES_TS.splitlines(), 1)
        ),
    )
    client_cf = ChangedFile(
        path="src/api-client.ts",
        status="added",
        lines=tuple(
            DiffLine(i, "added", line) for i, line in enumerate(SWAPI_API_CLIENT_TS.splitlines(), 1)
        ),
    )
    ctx = _context(changed_files=(starships_cf, vehicles_cf, client_cf))

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
                # Lines 1-2 of vehicles.ts do NOT contain idOrUrl!
                EvidenceRef(
                    block_id=file_block_id("src/tools/vehicles.ts"), start_line=1, end_line=2
                ),
            ],
        ),
    )
    non_tool_external_ref = _candidate(
        "c-non-tool-ref",
        _finding(
            path="src/tools/starships.ts",
            line=6,
            title="Inconsistent parameter naming in get_starship",
            body="The tool uses `id`, while other tools in the codebase use `idOrUrl`.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=6, end_line=6
                ),
                # api-client.ts is not under src/tools/*
                EvidenceRef(block_id=file_block_id("src/api-client.ts"), start_line=8, end_line=9),
            ],
        ),
    )
    valid_external_ref = _candidate(
        "c-good-ref",
        _finding(
            path="src/tools/starships.ts",
            line=6,
            title="Inconsistent parameter naming",
            body="Other tools use `idOrUrl`.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/starships.ts"), start_line=6, end_line=6
                ),
                EvidenceRef(
                    block_id=file_block_id("src/tools/vehicles.ts"), start_line=6, end_line=7
                ),
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
        non_tool_external_ref,
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
        ("c-non-tool-ref", "insufficient_context"),
    ]


def test_python_pyi_ast_audit_contradicts_imported_symbol() -> None:
    """Python .pyi files are audited for imported symbols via AST."""
    py_source = b"from pkg import Client as ApiClient\n"
    finding = _finding(
        path="app/svc.pyi",
        line=1,
        title="Missing import for ApiClient",
        body="Missing import.",
    )
    assert audit_candidate_claim(finding, py_source).status == "contradicted"


def test_python_try_except_body_contradicts_unhandled_claim() -> None:
    """Python try/except blocks contradict unhandled error claims inside try body."""
    py_source = (
        b"def run():\n    try:\n        call_api()\n    except ValueError:\n        recover()\n"
    )
    finding = _finding(
        path="app/svc.py",
        line=3,
        title="No try/except around call_api",
        body="No handler.",
    )
    assert audit_candidate_claim(finding, py_source).status == "contradicted"


@pytest.mark.parametrize(
    ("line", "title"),
    [
        (6, "No try/except in recover"),
        (8, "No try/except in cleanup"),
    ],
)
def test_python_try_except_catch_and_finally_lines_remain_supported(line: int, title: str) -> None:
    """Lines in Python except and finally clauses are not treated as inside try body."""
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
    finding = _finding(
        path="app/svc.py",
        line=line,
        title=title,
        body="Uncaught error.",
    )
    assert audit_candidate_claim(finding, py_source).status == "supported"


def test_jsx_import_audit_contradicts_imported_symbol() -> None:
    """JSX files are audited via Tree-sitter for imported symbols."""
    jsx_src = b"import { Widget } from './w';\nexport const App = () => <Widget />;\n"
    finding = _finding(
        path="src/App.jsx",
        line=2,
        title="Widget is not imported",
        body="Missing Widget.",
    )
    assert audit_candidate_claim(finding, jsx_src, claim_syntax=INSPECTOR).status == "contradicted"


def test_go_unsupported_language_fails_open_as_supported() -> None:
    """Unsupported languages like Go fail open with supported status."""
    go_src = b"package main\nfunc f() {}\n"
    finding = _finding(
        path="main.go",
        line=2,
        title="fmt is not imported",
        body="Missing fmt.",
    )
    assert audit_candidate_claim(finding, go_src, claim_syntax=INSPECTOR).status == "supported"


def test_python_syntax_error_fails_open_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Python syntax errors fail open as supported and log a warning."""
    caplog.set_level(logging.WARNING)
    bad_py = b"def broken(:\n"
    finding = _finding(
        path="app/bad.py",
        line=1,
        title="Foo is not imported",
        body="Missing Foo.",
    )
    assert audit_candidate_claim(finding, bad_py).status == "supported"
    assert any("Python AST parse failed" in rec.message for rec in caplog.records)


def test_oversized_source_file_fails_open_as_supported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Source files exceeding MAX_SOURCE_BYTES fail open and log a warning."""
    caplog.set_level(logging.WARNING)
    oversized = b"x = 1\n" + b"a" * MAX_SOURCE_BYTES
    finding = _finding(
        path="src/big.ts",
        line=1,
        title="Foo is not imported",
        body="Missing Foo.",
    )
    assert audit_candidate_claim(finding, oversized, claim_syntax=INSPECTOR).status == "supported"
    assert any("exceeds" in rec.message for rec in caplog.records)


def test_python_lambda_in_call_argument_does_not_disable_try_body_check() -> None:
    """A trailing lambda in a call argument does not prevent enclosing try detection."""
    py_source = b"try:\n    x = sorted(items, key=lambda i: i.k)\nexcept Exception:\n    pass\n"
    finding = _finding(
        path="app/a.py",
        line=2,
        title="Call not in try/except",
        body="sorted(...) call is not wrapped in a try/except.",
    )
    assert audit_candidate_claim(finding, py_source).status == "contradicted"


def test_comparative_claim_with_empty_ranges_or_no_lines_fails_as_insufficient_context() -> None:
    """Empty line map fails as insufficient context and avoids whole-block searches."""
    from app.modules.reviews.application.claim_verifier import ContextBlockSnippet

    finding = _finding(
        path="src/tools/starships.ts",
        line=6,
        title="Inconsistent parameter naming",
        body="Other tools use `idOrUrl`.",
        evidence_refs=[
            EvidenceRef(
                block_id="b-empty-lines",
                start_line=1,
                end_line=5,
            ),
        ],
    )
    snippets = {
        "b-empty-lines": ContextBlockSnippet(
            path="src/tools/other.ts",
            lines={},
            text="function test() { const idOrUrl = 1; }\n",
        ),
    }
    result = audit_candidate_claim(
        finding,
        b"const x = 1;\n",
        context_blocks=snippets,
    )
    assert result.status == "insufficient_context"


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
    """SWAPI security/coverage findings and out-of-scope logical claims pass audit."""
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
            line=94,
            title="Missing validation for exactly one identifier",
            body="The check does not enforce that exactly one of film_id or episode_id is passed.",
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("src/tools/films.ts"), start_line=94, end_line=96
                )
            ],
        ),
    )
    # Test error branch findings from swapi-mcp-example#3
    test_search_people_err = _candidate(
        "c-test-api",
        _finding(
            path="test/api-client.test.ts",
            line=134,
            title="Test does not verify ApiError properties on 404 response in searchPeople",
            body=(
                "The test checks that searchPeople throws an error, but it does not assert "
                "that the thrown error is an instance of ApiError or check its status code."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("test/api-client.test.ts"), start_line=130, end_line=135
                )
            ],
        ),
    )
    test_get_film_err = _candidate(
        "c-test-tools",
        _finding(
            path="test/tools.test.ts",
            line=170,
            title="Test does not verify error result on invalid params in get_film",
            body=(
                "The test error branch asserts toErrorResult is called but does not verify "
                "the McpError code and message in the returned result structure."
            ),
            evidence_refs=[
                EvidenceRef(
                    block_id=file_block_id("test/tools.test.ts"), start_line=165, end_line=175
                )
            ],
        ),
    )

    supported, rejected = filter_verified_candidates(
        candidates=(ssrf, id_from_url, exactly_one, test_search_people_err, test_get_film_err),
        decisions=(
            _supported_decision(ssrf),
            _supported_decision(id_from_url),
            _supported_decision(exactly_one),
            _supported_decision(test_search_people_err),
            _supported_decision(test_get_film_err),
        ),
        context=ctx,
        claim_syntax=INSPECTOR,
    )
    assert rejected == []
    assert [c.candidate_id for c in supported] == [
        "c-ssrf",
        "c-id-url",
        "c-logic",
        "c-test-api",
        "c-test-tools",
    ]


class _PipelineStub(_AcceptingCheckpointPublisher):
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
        {ln: ("context" if ln != 94 else "added", films_lines[ln - 1]) for ln in range(93, 115)},
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
                "line": 95,
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
                        "start_line": 94,
                        "end_line": 98,
                    }
                ],
            },
            {
                "path": "src/tools/films.ts",
                "line": 103,
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
                        "start_line": 103,
                        "end_line": 103,
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


def test_multi_symbol_import_claims() -> None:
    """'A and B are not imported' is contradicted ONLY if both A and B are imported."""
    # Source only imports ErrorCode, McpError is missing
    source_one = b'import { ErrorCode } from "sdk";\nexport function f() {}\n'
    # Source imports both
    source_both = b'import { ErrorCode, McpError } from "sdk";\nexport function f() {}\n'

    finding = _finding(
        path="src/tools.ts",
        line=5,
        title="Missing imports",
        body="`McpError` and `ErrorCode` are not imported.",
    )

    # When only ErrorCode is imported, claim is partially true -> supported (retained)
    res_one = audit_candidate_claim(finding, source_one, claim_syntax=INSPECTOR)
    assert res_one.status == "supported"

    # When both are imported, claim is contradicted -> dropped
    res_both = audit_candidate_claim(finding, source_both, claim_syntax=INSPECTOR)
    assert res_both.status == "contradicted"


def test_comparative_framework_name_without_quotes_remains_supported() -> None:
    """'other endpoints use Depends' should not trigger comparative param check."""
    finding = _finding(
        path="app/api.py",
        line=10,
        title="FastAPI dependency injection",
        body="other endpoints use Depends for authentication.",
    )
    res = audit_candidate_claim(finding, b"def api(): pass\n")
    assert res.status == "supported"


def test_security_finding_with_incidental_comparative_remark_remains_supported() -> None:
    """Primary security defect finding is retained even if incidental remark lacks external ref."""
    finding = _finding(
        path="src/tools/films.ts",
        line=100,
        title="SSRF vulnerability in fetchFilm: user URL fetched without validation",
        body=(
            "The URL is fetched directly without domain validation. "
            "Other tools use validateUrl parameter."
        ),
        evidence_refs=[
            EvidenceRef(block_id=file_block_id("src/tools/films.ts"), start_line=100, end_line=105)
        ],
    )
    res = audit_candidate_claim(finding, SWAPI_FILMS_TS.encode("utf-8"), claim_syntax=INSPECTOR)
    assert res.status == "supported"


def test_python_try_header_line_is_recognized_as_enclosed() -> None:
    """Python try: header line is treated as enclosed by try body, matching TypeScript behavior."""
    py_source = (
        b"def worker():\n    try:\n        call_external()\n    except Exception:\n        pass\n"
    )
    # Line 2 is the 'try:' statement itself
    finding = _finding(
        path="app/worker.py",
        line=2,
        title="Unhandled call_external",
        body="call_external is not wrapped in a try/except block.",
    )
    res = audit_candidate_claim(finding, py_source)
    assert res.status == "contradicted"


def test_inconsistent_resource_parameter_naming_is_not_treated_as_primary_defect() -> None:
    """Title with substring 'rce' (in 'resource') does not trigger primary defect bypass."""
    finding = _finding(
        path="src/tools/films.ts",
        line=100,
        title="Inconsistent resource parameter naming",
        body="Other tools use idOrUrl parameter.",
        evidence_refs=[
            EvidenceRef(block_id=file_block_id("src/tools/films.ts"), start_line=100, end_line=105)
        ],
    )
    res = audit_candidate_claim(finding, SWAPI_FILMS_TS.encode("utf-8"), claim_syntax=INSPECTOR)
    assert res.status == "insufficient_context"


def test_primary_defect_finding_with_bad_external_ref_is_not_bypassed() -> None:
    """When external references are present but omit param, primary defect does not bypass."""
    other_block = ContextBlockSnippet(
        path="src/tools/vehicles.ts",
        text="export const x = 1;",
        lines={1: "export const x = 1;"},
    )
    bid = file_block_id("src/tools/vehicles.ts")
    finding = _finding(
        path="src/tools/films.ts",
        line=100,
        title="SSRF vulnerability in fetchFilm",
        body="Other tools use idOrUrl parameter.",
        evidence_refs=[
            EvidenceRef(block_id=file_block_id("src/tools/films.ts"), start_line=100, end_line=105),
            EvidenceRef(block_id=bid, start_line=1, end_line=1),
        ],
    )
    res = audit_candidate_claim(
        finding,
        SWAPI_FILMS_TS.encode("utf-8"),
        claim_syntax=INSPECTOR,
        context_blocks={bid: other_block},
    )
    assert res.status == "insufficient_context"
    assert "omit it" in (res.reason or "")

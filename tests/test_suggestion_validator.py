"""Tests for suggestion AST validation, duplicate detection, and validator logging (#444)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    AstLanguage,
    SuggestionRejectionReason,
    SuggestionValidationRequest,
    SuggestionValidationResult,
    language_for_path,
)
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffOmissionReason,
    VcsProviderName,
    build_diff_map,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_output import (
    ReviewFinding,
    ReviewOutput,
    ReviewSummary,
)
from app.modules.reviews.application.suggestion_validator import (
    preload_suggestion_blobs,
    validate_review_suggestions,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.suggestion_ast import (
    apply_suggestion_to_text,
    validate_suggestion_tree,
)
from tests.verifier_pipeline_fakes import InProcessSuggestionParser

_FIXTURES = Path(__file__).parent / "fixtures" / "swapi"
_REPO_ID = UUID("00000000-0000-4000-8000-000000000444")
_RUN_ID = UUID("00000000-0000-4000-8000-000000000999")
_BLOB_SHA = "b" * 40


def _diff_map_for(
    path: str,
    *,
    status: str = "modified",
    blob_sha: str | None = _BLOB_SHA,
    old_path: str | None = None,
    omission_reason: DiffOmissionReason | None = None,
) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(
            installation_external_id=1, repository_full_name="acme/swapi", number=3
        ),
        head_sha="h" * 40,
        base_sha="a" * 40,
        meta=PullRequestMeta(
            title="SWAPI PR",
            description=None,
            author="octo",
            source_branch="feat",
            target_branch="main",
            labels=(),
            files_changed=1,
            lines_added=1,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    vcs_file = VcsFile(
        filename=path,
        status=status,  # type: ignore[arg-type]
        blob_sha=blob_sha,
        previous_filename=old_path,
        additions=1,
        deletions=0,
        changes=1,
        patch="@@ -1,1 +1,1 @@\n-old\n+new",
    )
    dm = build_diff_map(
        pull_request,
        (vcs_file,),
        provider=VcsProviderName.GITHUB,
        repository_id=_REPO_ID,
        diff_old_sha="a" * 40,
    )
    if omission_reason is not None:
        return replace(dm, files=(replace(dm.files[0], omission_reason=omission_reason),))
    return dm


def _output(
    path: str, line: int, suggestion: str, *, start_line: int | None = None
) -> ReviewOutput:
    return ReviewOutput(
        findings=[
            ReviewFinding(
                path=path,
                start_line=start_line,
                line=line,
                severity="medium",
                category="correctness",
                title="Finding",
                body="Detailed review comment.",
                suggestion=suggestion,
                confidence=0.9,
                rule_name=None,
            )
        ],
        summary=ReviewSummary(problem="One issue.", done_well="Clean.", effort="small"),
    )


class TestApplySuggestionToText:
    @pytest.mark.parametrize(
        ("original", "start_line", "line", "suggestion", "expected"),
        [
            pytest.param("l1\nl2\nl3\n", None, 2, "new2", "l1\nnew2\nl3\n", id="single_none_start"),
            pytest.param(
                "l1\nl2\nl3\n", 2, 2, "new2", "l1\nnew2\nl3\n", id="single_explicit_start"
            ),
            pytest.param(
                "l1\nl2\nl3\nl4\nl5\n", 2, 4, "a\nb", "l1\na\nb\nl5\n", id="multiline_range"
            ),
            pytest.param("l1\nl2\n", 1, 1, "head", "head\nl2\n", id="first_line"),
            pytest.param("l1\nl2\n", 2, 2, "foot", "l1\nfoot\n", id="last_line_trailing_nl"),
            pytest.param("l1\nl2", 2, 2, "foot", "l1\nfoot", id="last_line_no_trailing_nl"),
            pytest.param(
                "def f():\n    pass\n\f\ndef g():\n    return 3\n",
                5,
                5,
                "    return 4",
                "def f():\n    pass\n\f\ndef g():\n    return 4\n",
                id="form_feed_strict_lf",
            ),
            pytest.param(
                "const s = 'text\u2028more';\nconst y = 1;\nreturn 2;\n",
                3,
                3,
                "return 3;",
                "const s = 'text\u2028more';\nconst y = 1;\nreturn 3;\n",
                id="unicode_ls_strict_lf",
            ),
            pytest.param(
                "line 1\r\nline 2\r\nline 3\r\n",
                2,
                2,
                "new line 2",
                "line 1\r\nnew line 2\r\nline 3\r\n",
                id="crlf_preserved",
            ),
            pytest.param(
                "l1\nl2\nl3\n", 2, 2, "new2\n", "l1\nnew2\n\nl3\n", id="trailing_nl_in_suggestion"
            ),
        ],
    )
    def test_apply_replacements(
        self, original: str, start_line: int | None, line: int, suggestion: str, expected: str
    ) -> None:
        assert apply_suggestion_to_text(original, start_line, line, suggestion) == expected

    @pytest.mark.parametrize(
        ("start_line", "line", "pattern"),
        [
            (None, 10, "out of range"),
            (3, 2, "must be <= line"),
            (0, 1, "1-based"),
        ],
    )
    def test_invalid_line_numbers_raise_value_error(
        self, start_line: int | None, line: int, pattern: str
    ) -> None:
        with pytest.raises(ValueError, match=pattern):
            apply_suggestion_to_text("line 1\nline 2\n", start_line, line, "x")


class TestSyntaxAndDuplicateAst:
    @pytest.mark.parametrize(
        ("language", "path_check", "code", "start_line", "line", "suggestion", "expected_reason"),
        [
            pytest.param(
                AstLanguage.TYPESCRIPT,
                "src/tools/films.ts",
                (_FIXTURES / "films_858e1c1.ts").read_text(encoding="utf-8"),
                None,
                96,
                'import { McpError, ErrorCode } from "@modelcontextprotocol/sdk/server/mcp.js";',
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="films_858e1c1_l96_invalid_scoped_import",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                "src/a.ts",
                "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
                2,
                2,
                "  // Comment\n  return a + b;",
                None,
                id="valid_typescript",
            ),
            pytest.param(
                AstLanguage.TSX,
                "src/App.tsx",
                (
                    "export function App() {\n"
                    "  const ok = true;\n"
                    "  if (ok) {\n"
                    "    return <div />;\n"
                    "  }\n"
                    "}\n"
                ),
                3,
                3,
                "import React from 'react';",
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="tsx_scoped_import_caught_by_tree_sitter_errors",
            ),
            pytest.param(
                AstLanguage.PYTHON,
                "src/a.py",
                "def total(items):\n    return 0\n",
                2,
                2,
                "    return sum(i.price for i in items)",
                None,
                id="valid_python",
            ),
            pytest.param(
                AstLanguage.PYTHON,
                "src/a.py",
                "def check(x):\n    if x > 0:\n        return True\n    return False\n",
                2,
                3,
                "    if x > 0:\n    return True",
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="python_indentation_syntax_error",
            ),
            pytest.param(
                AstLanguage.PYTHON,
                "src/a.py",
                "def broken(:\n    x = 1\n    return x\n",
                2,
                2,
                "    x = 2",
                None,
                id="python_already_broken_before_keeps_valid_suggestion",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                "types/mod.d.ts",
                'declare module "foo" {\n  import { B } from "bar";\n  export const b: B;\n}\n',
                2,
                2,
                '  import { B, C } from "bar";',
                None,
                id="dts_declare_module_import_allowed",
            ),
            pytest.param(
                AstLanguage.JAVASCRIPT,
                "src/calc.js",
                "function add(a, b) {\n  return a + b;\n}\n",
                2,
                2,
                "  return a + (b +;\n",
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="javascript_syntax_error_stripped",
            ),
            pytest.param(
                AstLanguage.JAVASCRIPT,
                "src/calc.js",
                "function add(a, b) {\n  return a + b;\n}\n",
                2,
                2,
                "  return a + b + 1;",
                None,
                id="javascript_valid_retained",
            ),
            pytest.param(
                AstLanguage.JAVASCRIPT,
                "src/Component.jsx",
                "export function Item() {\n  return <span>item</span>;\n}\n",
                2,
                2,
                "  return <span>item<;\n",
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="jsx_syntax_error_stripped",
            ),
            pytest.param(
                AstLanguage.JAVASCRIPT,
                "src/Component.jsx",
                "export function Item() {\n  return <span>item</span>;\n}\n",
                2,
                2,
                "  return <span>updated item</span>;",
                None,
                id="jsx_valid_retained",
            ),
            pytest.param(
                AstLanguage.GO,
                "pkg/math.go",
                "package math\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n",
                4,
                4,
                "\treturn a + (b +\n",
                SuggestionRejectionReason.SYNTAX_ERROR,
                id="go_syntax_error_stripped",
            ),
            pytest.param(
                AstLanguage.GO,
                "pkg/math.go",
                "package math\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n",
                4,
                4,
                "\treturn a + b + 1",
                None,
                id="go_valid_retained",
            ),
        ],
    )
    def test_syntax_cases(
        self,
        language: AstLanguage,
        path_check: str,
        code: str,
        start_line: int | None,
        line: int,
        suggestion: str,
        expected_reason: SuggestionRejectionReason | None,
    ) -> None:
        assert language_for_path(path_check) == language
        res = validate_suggestion_tree(
            language, code.encode("utf-8"), start_line=start_line, line=line, suggestion=suggestion
        )
        assert res.is_valid is (expected_reason is None)
        assert res.reason is expected_reason

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("src/components/App.jsx", AstLanguage.JAVASCRIPT),
            ("stubs/typing.pyi", AstLanguage.PYTHON),
            ("README.md", None),
        ],
    )
    def test_language_for_path(self, path: str, expected: AstLanguage | None) -> None:
        assert language_for_path(path) is expected

    def test_deep_js_else_if_and_python_unary_stress(self) -> None:
        branches = "\n".join(f"  else if (x === {i}) {{ return {i}; }}" for i in range(1, 501))
        js_code = f"function test(x) {{\n  if (x === 0) {{ return 0; }}\n{branches}\n}}\n"
        assert validate_suggestion_tree(
            AstLanguage.JAVASCRIPT, js_code.encode("utf-8"), 2, 2, "  if (x === 0) { return 42; }"
        ).is_valid

        py_code = f"x = {'-' * 200_000}1\n"
        res = validate_suggestion_tree(AstLanguage.PYTHON, py_code.encode("utf-8"), 1, 1, "x = 42")
        assert res.is_valid is True
        assert res.reason is None

    @pytest.mark.parametrize(
        ("language", "code", "start_line", "line", "suggestion", "should_be_valid"),
        [
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function f(a: boolean, b: boolean) {\n"
                    "  if ((a) && (b)) {\n"
                    "    return 1;\n"
                    "  }\n"
                    "}\n"
                ),
                3,
                3,
                "    if ((a) && (b)) { return 2; }",
                False,
                id="compound_parenthesized_cond_duplicate",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function p(x: boolean) {\n  if (x) { if (x) { return 1; } }\n}\n"
                    "function t(y: boolean) {\n  if (y) {\n    return 2;\n  }\n}\n"
                ),
                6,
                6,
                "    if (y) { return 3; }",
                False,
                id="existing_dup_in_other_fn_does_not_mask_new_dup",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function p(x: boolean) {\n  if (x) { if (x) { return 1; } }\n}\n"
                    "function t(y: boolean) {\n  return y;\n}\n"
                ),
                5,
                5,
                "  return !y;",
                True,
                id="existing_dup_untouched_is_kept",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function f(items: string[]) {\n"
                    "  if (items.length) {\n"
                    "    return items[0];\n"
                    "  }\n"
                    "}\n"
                ),
                3,
                3,
                "    process(items);\n    items = next(items);\n    if (items.length) { return; }",
                True,
                id="intervening_statement_kept",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function f(self: { active: boolean }) {\n"
                    "  if (self.active) {\n"
                    "    go();\n"
                    "  }\n"
                    "}\n"
                ),
                3,
                3,
                "    setTimeout(() => {\n      if (self.active) { run(); }\n    }, 100);",
                True,
                id="nested_if_inside_callback_kept",
            ),
            pytest.param(
                AstLanguage.TYPESCRIPT,
                (
                    "function f(x: { ok: boolean }, ys: number[]) {\n"
                    "  if (x.ok) {\n"
                    "    go();\n"
                    "  }\n"
                    "}\n"
                ),
                3,
                3,
                "    for (const y of ys) {\n      run(y);\n      if (x.ok) { stop(); }\n    }",
                True,
                id="nested_if_inside_for_loop_kept",
            ),
            pytest.param(
                AstLanguage.GO,
                "package p\nfunc f() {\n\tif err := a(); err != nil {\n\t\treturn\n\t}\n}\n",
                4,
                4,
                "\t\tif err := b(); err != nil {\n\t\t\treturn\n\t\t}",
                True,
                id="go_different_initializer_kept",
            ),
            pytest.param(
                AstLanguage.GO,
                "package p\nfunc f() {\n\tif err := a(); err != nil {\n\t\treturn\n\t}\n}\n",
                4,
                4,
                "\t\tif err != nil {\n\t\t\treturn\n\t\t}",
                False,
                id="go_same_err_without_reinit_rejected",
            ),
            pytest.param(
                AstLanguage.JAVASCRIPT,
                "function f(a) {\n  if (a) {\n    doOne();\n  }\n}\n",
                4,
                4,
                "  } else if (a) {\n    doTwo();\n  }",
                True,
                id="js_else_if_same_condition_kept_like_python_elif",
            ),
            pytest.param(
                AstLanguage.PYTHON,
                "def f(a):\n    if a:\n        return 1\n",
                3,
                3,
                "        return 1\n    elif a:\n        return 2",
                True,
                id="python_elif_same_condition_kept",
            ),
            pytest.param(
                AstLanguage.PYTHON,
                "def f(x, y):\n    if x:\n        return 1\n    elif y:\n        return 2\n",
                5,
                5,
                "        if y:\n            return 2",
                False,
                id="python_duplicate_inside_elif_rejected",
            ),
        ],
    )
    def test_duplicate_nested_condition_matrix(
        self,
        language: AstLanguage,
        code: str,
        start_line: int,
        line: int,
        suggestion: str,
        should_be_valid: bool,
    ) -> None:
        res = validate_suggestion_tree(language, code.encode("utf-8"), start_line, line, suggestion)
        assert res.is_valid is should_be_valid
        if not should_be_valid:
            assert res.reason is SuggestionRejectionReason.DUPLICATE_CONDITION


class TestValidatorAllNineReasonsAndCaplog:
    @pytest.mark.parametrize(
        "mode",
        [
            "syntax_error",
            "duplicate_condition",
            "application_failed",
            "unsupported_language",
            "file_unavailable_miss",
            "file_unavailable_expired",
            "file_unavailable_omitted",
            "file_unavailable_old_path_rename",
            "file_unavailable_cache_error",
            "non_utf8",
            "file_too_large",
            "ast_timeout",
            "worker_error_exception",
            "worker_error_missing_result",
        ],
    )
    def test_all_nine_rejection_and_retention_reasons(
        self, caplog: pytest.LogCaptureFixture, mode: str
    ) -> None:
        path = "README.md" if mode == "unsupported_language" else "src/a.ts"
        line = 999 if mode == "application_failed" else 2
        suggestion = "broken((" if mode == "syntax_error" else "  return 2;"
        if mode == "duplicate_condition":
            code = "function f(x: boolean) {\n  if (x) {\n    return 1;\n  }\n}\n"
            line = 3
            suggestion = "    if (x) { return 2; }"
        else:
            code = "export function f() {\n  return 1;\n}\n"

        diff_map = _diff_map_for(
            "src/new.ts" if mode == "file_unavailable_old_path_rename" else path,
            status="renamed" if mode == "file_unavailable_old_path_rename" else "modified",
            old_path="src/a.ts" if mode == "file_unavailable_old_path_rename" else None,
            omission_reason=(
                DiffOmissionReason.UNSUPPORTED_PATH if mode == "file_unavailable_omitted" else None
            ),
        )

        cache = InMemoryBlobCache()
        preloaded: dict[str, bytes | None] | None = None
        if mode == "non_utf8":
            preloaded = {path: b"\xff\xfe\x00\x01"}
        elif mode == "file_too_large":
            preloaded = {path: b"x" * (MAX_SOURCE_BYTES + 1)}
        elif mode == "file_unavailable_expired":

            class _ExpiredCache(InMemoryBlobCache):
                async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
                    return BlobCacheEntry(BlobCacheStatus.EXPIRED, code)

            cache = _ExpiredCache()
        elif mode == "file_unavailable_cache_error":

            class _FailingCache(InMemoryBlobCache):
                async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
                    raise TimeoutError("db timeout")

            cache = _FailingCache()
        elif mode != "file_unavailable_miss":
            asyncio.run(cache.put(BlobCacheKey(_REPO_ID, _BLOB_SHA), code, ttl=timedelta(hours=1)))

        parser: object = InProcessSuggestionParser()
        if mode == "ast_timeout":

            class _TimeoutParser:
                async def validate_suggestions(
                    self, requests: tuple[SuggestionValidationRequest, ...]
                ) -> dict[str, SuggestionValidationResult]:
                    return {
                        r.key: SuggestionValidationResult(
                            True, SuggestionRejectionReason.AST_TIMEOUT, "timeout"
                        )
                        for r in requests
                    }

                async def aclose(self) -> None:
                    return None

            parser = _TimeoutParser()
        elif mode == "worker_error_exception":

            class _BoomParser:
                async def validate_suggestions(
                    self, requests: tuple[SuggestionValidationRequest, ...]
                ) -> dict[str, SuggestionValidationResult]:
                    raise RuntimeError("worker crashed")

                async def aclose(self) -> None:
                    return None

            parser = _BoomParser()
        elif mode == "worker_error_missing_result":

            class _EmptyParser:
                async def validate_suggestions(
                    self, requests: tuple[SuggestionValidationRequest, ...]
                ) -> dict[str, SuggestionValidationResult]:
                    return {}

                async def aclose(self) -> None:
                    return None

            parser = _EmptyParser()

        expected_reason = (
            mode
            if mode
            in {
                "syntax_error",
                "duplicate_condition",
                "application_failed",
                "unsupported_language",
                "non_utf8",
                "file_too_large",
                "ast_timeout",
            }
            else ("worker_error" if mode.startswith("worker_error") else "file_unavailable")
        )
        should_strip = mode in {"syntax_error", "duplicate_condition", "application_failed"}

        if preloaded is None:
            preloaded = asyncio.run(
                preload_suggestion_blobs(
                    _output(path, line, suggestion), diff_map=diff_map, blob_cache=cache
                )
            )

        with caplog.at_level(logging.INFO):
            res = asyncio.run(
                validate_review_suggestions(
                    _output(path, line, suggestion),
                    diff_map=diff_map,
                    preloaded_blobs=preloaded,
                    parser=parser,  # type: ignore[arg-type]
                    run_id=_RUN_ID,
                )
            )

        if should_strip:
            assert res.findings[0].suggestion is None
        else:
            assert res.findings[0].suggestion == suggestion

        info_records = [
            rec
            for rec in caplog.records
            if rec.name == "app.modules.reviews.application.suggestion_validator"
            and rec.levelno == logging.INFO
        ]
        assert len(info_records) == 1
        msg = info_records[0].getMessage()
        assert f"run_id={_RUN_ID}" in msg
        assert f"path={path!r}" in msg
        assert f"line={line}" in msg
        assert f"reason={expected_reason}" in msg

    def test_valid_suggestion_emits_no_log_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        code = "export function f(): number {\n  return 1;\n}\n"
        diff_map = _diff_map_for("src/a.ts")
        cache = InMemoryBlobCache()
        asyncio.run(cache.put(BlobCacheKey(_REPO_ID, _BLOB_SHA), code, ttl=timedelta(hours=1)))
        output = _output("src/a.ts", 2, "  return 2;")
        preloaded = asyncio.run(
            preload_suggestion_blobs(output, diff_map=diff_map, blob_cache=cache)
        )
        with caplog.at_level(logging.INFO):
            res = asyncio.run(
                validate_review_suggestions(
                    output,
                    diff_map=diff_map,
                    preloaded_blobs=preloaded,
                    parser=InProcessSuggestionParser(),
                    run_id=_RUN_ID,
                )
            )
        assert res.findings[0].suggestion == "  return 2;"
        assert not [r for r in caplog.records if "suggestion_validator" in r.name]

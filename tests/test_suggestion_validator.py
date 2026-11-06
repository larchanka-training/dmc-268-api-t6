"""Tests for suggestion validation against tree-sitter AST and duplicate conditions (#444)."""

import pytest

from app.modules.reviews.application.ast_symbols import (
    AstLanguage,
    SuggestionRejectionReason,
    language_for_path,
)
from app.modules.reviews.infrastructure.suggestion_ast import (
    apply_suggestion_to_text,
    validate_suggestion_tree,
)


class TestApplySuggestionToText:
    """Test text replacement emulation matching GitHub suggestion behavior strictly with \\n."""

    def test_replace_single_line_none_start(self) -> None:
        original = "line 1\nline 2\nline 3\n"
        result = apply_suggestion_to_text(
            original, start_line=None, line=2, suggestion="new line 2"
        )
        assert result == "line 1\nnew line 2\nline 3\n"

    def test_replace_single_line_explicit_start(self) -> None:
        original = "line 1\nline 2\nline 3\n"
        result = apply_suggestion_to_text(original, start_line=2, line=2, suggestion="new line 2")
        assert result == "line 1\nnew line 2\nline 3\n"

    def test_replace_multiline_range(self) -> None:
        original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        result = apply_suggestion_to_text(
            original, start_line=2, line=4, suggestion="replacement A\nreplacement B"
        )
        assert result == "line 1\nreplacement A\nreplacement B\nline 5\n"

    def test_replace_first_line(self) -> None:
        original = "line 1\nline 2\n"
        result = apply_suggestion_to_text(original, start_line=1, line=1, suggestion="header")
        assert result == "header\nline 2\n"

    def test_replace_last_line(self) -> None:
        original = "line 1\nline 2\n"
        result = apply_suggestion_to_text(original, start_line=2, line=2, suggestion="footer")
        assert result == "line 1\nfooter\n"

    def test_replace_last_line_without_trailing_newline(self) -> None:
        original = "line 1\nline 2"
        result = apply_suggestion_to_text(original, start_line=2, line=2, suggestion="footer")
        assert result == "line 1\nfooter"

    def test_form_feed_does_not_shift_line_numbers(self) -> None:
        # \f must not split lines; line 5 must remain line 5
        original = "def f():\n    pass\n\f\ndef g():\n    return 3\n"
        result = apply_suggestion_to_text(original, start_line=5, line=5, suggestion="    return 4")
        assert result == "def f():\n    pass\n\f\ndef g():\n    return 4\n"

    def test_unicode_paragraph_separator_does_not_shift_lines(self) -> None:
        # \u2028 inside string literal on line 1 must not split lines
        original = "const s = 'text\u2028more';\nconst y = 1;\nreturn 2;\n"
        result = apply_suggestion_to_text(original, start_line=3, line=3, suggestion="return 3;")
        assert result == "const s = 'text\u2028more';\nconst y = 1;\nreturn 3;\n"

    def test_crlf_preserved(self) -> None:
        original = "line 1\r\nline 2\r\nline 3\r\n"
        result = apply_suggestion_to_text(original, start_line=2, line=2, suggestion="new line 2")
        assert result == "line 1\r\nnew line 2\r\nline 3\r\n"

    def test_trailing_newline_in_suggestion(self) -> None:
        original = "line 1\nline 2\nline 3\n"
        result = apply_suggestion_to_text(original, start_line=2, line=2, suggestion="new line 2\n")
        assert result == "line 1\nnew line 2\n\nline 3\n"

    def test_invalid_line_numbers_raise(self) -> None:
        original = "line 1\nline 2\n"
        with pytest.raises(ValueError, match="out of range"):
            apply_suggestion_to_text(original, start_line=None, line=10, suggestion="x")
        with pytest.raises(ValueError, match="must be <= line"):
            apply_suggestion_to_text(original, start_line=3, line=2, suggestion="x")
        with pytest.raises(ValueError, match="1-based"):
            apply_suggestion_to_text(original, start_line=0, line=1, suggestion="x")


class TestValidateSuggestionSyntax:
    """Test syntax validation using tree-sitter and AST parser."""

    def test_starships_ts_function_header_replaced_with_zod_schema(self) -> None:
        code = (
            "import { SWAPI_BASE } from './const';\n"
            "export async function getStarship(id: string): Promise<Starship> {\n"
            "  const response = await fetch(`${SWAPI_BASE}/starships/${id}/`);\n"
            "  return response.json();\n"
            "}\n"
        )
        suggestion = "const starshipSchema = z.object({ id: z.string() });"
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=None,
            line=2,
            suggestion=suggestion,
        )
        assert not res.is_valid
        assert res.reason == SuggestionRejectionReason.SYNTAX_ERROR

    def test_valid_typescript_suggestion(self) -> None:
        code = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
        suggestion = "  // Add two numbers\n  return a + b;"
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=2,
            line=2,
            suggestion=suggestion,
        )
        assert res.is_valid
        assert res.reason is None

    def test_valid_python_suggestion(self) -> None:
        code = (
            "def calculate_total(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item.price\n"
            "    return total\n"
        )
        suggestion = "    return sum(item.price for item in items)"
        res = validate_suggestion_tree(
            AstLanguage.PYTHON,
            code.encode("utf-8"),
            start_line=2,
            line=5,
            suggestion=suggestion,
        )
        assert res.is_valid
        assert res.reason is None

    def test_python_syntax_error_indentation_or_unclosed(self) -> None:
        code = "def check_value(x):\n    if x > 0:\n        return True\n    return False\n"
        suggestion = "    if x > 0:\n    return True"
        res = validate_suggestion_tree(
            AstLanguage.PYTHON,
            code.encode("utf-8"),
            start_line=2,
            line=3,
            suggestion=suggestion,
        )
        assert not res.is_valid
        assert res.reason == SuggestionRejectionReason.SYNTAX_ERROR

    def test_deep_else_if_chain_does_not_raise_recursion_error(self) -> None:
        # 500 else if branches in JS
        branches = "\n".join(f"  else if (x === {i}) {{ return {i}; }}" for i in range(1, 501))
        code = f"function test(x) {{\n  if (x === 0) {{ return 0; }}\n{branches}\n}}\n"
        suggestion = "  if (x === 0) { return 42; }"
        res = validate_suggestion_tree(
            AstLanguage.JAVASCRIPT,
            code.encode("utf-8"),
            start_line=2,
            line=2,
            suggestion=suggestion,
        )
        assert res.is_valid

    def test_python_deep_unary_minuses_handled_gracefully(self) -> None:
        # 200k minuses would cause MemoryError / RecursionError in ast.parse
        minuses = "-" * 200_000 + "1"
        code = f"x = {minuses}\n"
        suggestion = "x = 42"
        # Must not raise unhandled exception
        res = validate_suggestion_tree(
            AstLanguage.PYTHON,
            code.encode("utf-8"),
            start_line=1,
            line=1,
            suggestion=suggestion,
        )
        assert isinstance(res.is_valid, bool)

    def test_language_detection_supports_jsx_and_pyi(self) -> None:
        assert language_for_path("src/components/App.jsx") == AstLanguage.JAVASCRIPT
        assert language_for_path("stubs/typing.pyi") == AstLanguage.PYTHON


class TestDuplicateConditionDetection:
    """Test duplicate nested condition detection and scope isolation."""

    def test_common_ts_nested_duplicate_if_condition(self) -> None:
        code = (
            "export function handleApiError(error: unknown) {\n"
            "  if (!(error instanceof ApiError)) {\n"
            "    throw error;\n"
            "  }\n"
            "  return { status: error.status, message: error.message };\n"
            "}\n"
        )
        suggestion = "    if (!(error instanceof ApiError)) {\n      return { status: 500 };\n    }"
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=3,
            line=3,
            suggestion=suggestion,
        )
        assert not res.is_valid
        assert res.reason == SuggestionRejectionReason.DUPLICATE_CONDITION

    def test_duplicate_if_in_another_function_does_not_mask_new_duplicate(self) -> None:
        code = (
            "function prior(x: boolean) {\n"
            "  if (x) {\n"
            "    if (x) { return 1; }\n"
            "  }\n"
            "}\n"
            "function target(y: boolean) {\n"
            "  if (y) {\n"
            "    return 2;\n"
            "  }\n"
            "}\n"
        )
        suggestion = "    if (y) {\n      return 3;\n    }"
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=8,
            line=8,
            suggestion=suggestion,
        )
        assert not res.is_valid
        assert res.reason == SuggestionRejectionReason.DUPLICATE_CONDITION

    def test_intervening_statement_in_nested_if_is_valid(self) -> None:
        code = (
            "function processItems(items: string[]) {\n"
            "  if (items.length) {\n"
            "    const first = items[0];\n"
            "    return first;\n"
            "  }\n"
            "}\n"
        )
        suggestion = (
            "    process(items);\n"
            "    items = next(items);\n"
            "    if (items.length) {\n"
            "      return;\n"
            "    }"
        )
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=3,
            line=4,
            suggestion=suggestion,
        )
        assert res.is_valid

    def test_declare_module_with_import_in_dts_is_valid(self) -> None:
        code = 'declare module "foo" {\n  import { B } from "bar";\n  export const b: B;\n}\n'
        suggestion = '  import { B, C } from "bar";'
        res = validate_suggestion_tree(
            AstLanguage.TYPESCRIPT,
            code.encode("utf-8"),
            start_line=2,
            line=2,
            suggestion=suggestion,
        )
        assert res.is_valid

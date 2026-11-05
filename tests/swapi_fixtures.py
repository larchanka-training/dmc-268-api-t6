"""Shared fixtures and cases for SWAPI real-world broken suggestions (#444, #446)."""

from __future__ import annotations

from typing import NamedTuple


class SwapiSuggestionCase(NamedTuple):
    path: str
    status: str
    fixture_name: str
    start_line: int | None
    line: int
    suggestion: str
    expected_reason: str


SWAPI_SUGGESTION_CASES: tuple[SwapiSuggestionCase, ...] = (
    SwapiSuggestionCase(
        "src/tools/starships.ts",
        "added",
        "starships.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Starship resource ID or full SWAPI starship URL"),'
        ),
        "syntax_error",
    ),
    SwapiSuggestionCase(
        "src/tools/people.ts",
        "added",
        "people.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Character resource ID or full SWAPI character URL"),'
        ),
        "syntax_error",
    ),
    SwapiSuggestionCase(
        "src/tools/planets.ts",
        "added",
        "planets.ts",
        None,
        31,
        (
            "      id: z.union([z.number().int().positive(), z.string()]).describe("
            '"Planet resource ID or full SWAPI planet URL"),'
        ),
        "syntax_error",
    ),
    SwapiSuggestionCase(
        "src/tools/films.ts",
        "modified",
        "films_8bc8b5d.ts",
        93,
        94,
        (
            "      if (filmId === undefined && episodeId === undefined) {\n"
            "        throw new McpError(\n"
            "          ErrorCode.InvalidParams,\n"
            "          \"Provide exactly one of 'film_id' or 'episode_id' to identify"
            ' the episode.",\n'
            "        );\n"
            "      }"
        ),
        "syntax_error",
    ),
    SwapiSuggestionCase(
        "src/tools/films.ts",
        "modified",
        "films_858e1c1.ts",
        None,
        96,
        'import { McpError, ErrorCode } from "@modelcontextprotocol/sdk/server/mcp.js";',
        "syntax_error",
    ),
    SwapiSuggestionCase(
        "src/tools/common.ts",
        "added",
        "common.ts",
        None,
        17,
        (
            "  if (!(error instanceof ApiError)) {\n"
            "    return {\n"
            "      isError: true,\n"
            '      content: [{ type: "text" as const, text: `Internal error: '
            "${String(error)}` }],\n"
            "    };\n"
            "  }"
        ),
        "duplicate_condition",
    ),
)

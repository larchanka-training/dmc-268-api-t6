"""Exclusion classes of the graph indexer, in their order (ADR 0001 §7, §9; #142 AC4)."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.modules.code_index.application.exclusions import (
    INDEX_EXCLUSIONS_VERSION,
    ExclusionClass,
    IndexExclusions,
)
from app.modules.code_index.domain.codes import FAILED_ERROR_CODES
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_symbols import AstFacts, AstFailureReason, AstLanguage
from app.modules.reviews.application.review_ignore import IgnoreMatcher, parse_review_ignore

_MIB = 1_048_576
_KIB = 1024
_RULES = "# Rules\n\n```review-ignore\nlegacy/**\ndocs/**\n```\n"
_ZERO = dict.fromkeys(ExclusionClass, 0)


def _exclusions(rules: str = _RULES) -> IndexExclusions:
    return IndexExclusions(IgnoreMatcher(parse_review_ignore(rules)), IndexLimits())


def _entry(
    path: str, *, symlink: bool = False, size: int = 10
) -> tuple[ExclusionClass | None, dict[ExclusionClass, int]]:
    exclusions = _exclusions()
    found = exclusions.admit_entry(path, symlink=symlink, size=size)
    return found, dict(exclusions.counts)


def test_the_classes_are_in_the_plan_order() -> None:
    assert [item.value for item in ExclusionClass] == [
        "unsupported_path",
        "secret",
        "vendored",
        "generated",
        "unsupported_language",
        "ignored",
        "symlink",
        "over_read_limit",
        "over_parse_limit",
        "binary",
        "parse_failed",
    ]
    assert INDEX_EXCLUSIONS_VERSION == "index-exclusions/v1"


def test_limits_default_to_the_adr_values_and_their_failure_codes() -> None:
    assert asdict(IndexLimits()) == {
        "download_bytes": 104_857_600,
        "unpacked_bytes": 1_073_741_824,
        "archive_entries": 100_000,
        "read_bytes": 1_048_576,
        "parse_bytes": 262_144,
        "parsed_files": 20_000,
        "symbols": 500_000,
        "edges": 2_000_000,
        "deadline_seconds": 900,
        "incremental_files": 500,
    }
    for limit in ("download_bytes", "unpacked_bytes", "archive_entries", "parsed_files"):
        assert f"limit_exceeded:{limit}" in FAILED_ERROR_CODES
    for limit in ("symbols", "edges", "deadline"):
        assert f"limit_exceeded:{limit}" in FAILED_ERROR_CODES


def test_class_1_an_unsupported_path_is_checked_before_the_ignore_matcher() -> None:
    # The matcher raises PolicyError on such a path: it must never see one.
    for path in ("a//b.py", "a/../b.py", "a\\b.py", "a/\x01.py", "/abs.py", ""):
        assert _entry(path) == (
            ExclusionClass.UNSUPPORTED_PATH,
            {**_ZERO, ExclusionClass.UNSUPPORTED_PATH: 1},
        )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env.local",
        ".envrc.py",
        "certs/server.pem",
        "keys/deploy.KEY",
        "a.p12",
        "a.pfx",
        "store.jks",
        "vault.kdbx",
        "home/id_rsa",
        "home/id_ed25519.pub",
        "home/id_ecdsa",
        "home/id_dsa",
        "app/credentials.json",
        "app/secrets.py",
        "app/secret.ts",
        "home/.ssh/config.py",
        "deploy/.aws/env.go",
        "home/.gnupg/key.js",
        # A secret-like vendored path counts as a secret: class 2 comes before class 3.
        "vendor/secrets.go",
    ],
)
def test_class_2_secret_like_paths(path: str) -> None:
    assert _entry(path) == (ExclusionClass.SECRET, {**_ZERO, ExclusionClass.SECRET: 1})


@pytest.mark.parametrize(
    "path",
    [
        "vendor/github.com/x/a.go",
        "web/node_modules/lib/index.js",
        "third_party/a.py",
        "bower_components/b.js",
        "lib/python3.13/site-packages/c.py",
        ".venv/d.py",
        "venv/e.py",
        "ios/Pods/f.ts",
        # A vendored generated path counts as vendored: class 3 comes before class 4.
        "dist/vendor/a.js",
    ],
)
def test_class_3_vendored_directories(path: str) -> None:
    assert _entry(path) == (ExclusionClass.VENDORED, {**_ZERO, ExclusionClass.VENDORED: 1})


def test_class_3_matches_whole_directory_segments_only() -> None:
    for path in ("vendored/a.py", "src/vendor.py", "app/pods/handler.go", "my_venv/a.py"):
        assert _entry(path) == (None, _ZERO)


@pytest.mark.parametrize(
    "path",
    [
        "web/package-lock.json",
        "go.sum",
        "dist/app.js",
        "static/app.min.js",
        "api/service.pb.go",
        "ui/__snapshots__/a.test.ts.snap",
        "db/migrations/0001_snapshot.py",
        # A generated file of an unsupported language counts as generated: 4 before 5.
        "dist/readme.txt",
    ],
)
def test_class_4_generated_and_lock_files(path: str) -> None:
    assert _entry(path) == (ExclusionClass.GENERATED, {**_ZERO, ExclusionClass.GENERATED: 1})


def test_class_5_unsupported_language_comes_before_the_ignore_policy() -> None:
    for path in ("README.md", "docs/guide.md", "Makefile", "app/.hidden", "a.rs"):
        assert _entry(path) == (
            ExclusionClass.UNSUPPORTED_LANGUAGE,
            {**_ZERO, ExclusionClass.UNSUPPORTED_LANGUAGE: 1},
        )


def test_class_6_ignore_from_the_rules_document_comes_before_a_symlink() -> None:
    assert _entry("legacy/a.py", symlink=True) == (
        ExclusionClass.IGNORED,
        {**_ZERO, ExclusionClass.IGNORED: 1},
    )
    assert _entry("docs/conf.py") == (ExclusionClass.IGNORED, {**_ZERO, ExclusionClass.IGNORED: 1})


def test_class_7_a_symlink_comes_before_any_size() -> None:
    assert _entry("app/link.py", symlink=True, size=2 * _MIB) == (
        ExclusionClass.SYMLINK,
        {**_ZERO, ExclusionClass.SYMLINK: 1},
    )


def test_class_8_over_1_mib_is_never_read() -> None:
    assert _entry("app/big.py", size=_MIB + 1) == (
        ExclusionClass.OVER_READ_LIMIT,
        {**_ZERO, ExclusionClass.OVER_READ_LIMIT: 1},
    )


def test_class_9_over_256_kib_is_not_parsed() -> None:
    for size in (256 * _KIB + 1, _MIB):
        assert _entry("app/large.py", size=size) == (
            ExclusionClass.OVER_PARSE_LIMIT,
            {**_ZERO, ExclusionClass.OVER_PARSE_LIMIT: 1},
        )
    assert _entry("app/fits.py", size=256 * _KIB) == (None, _ZERO)


def test_class_10_binary_content() -> None:
    exclusions = _exclusions()

    assert exclusions.admit_entry("app/data.py", symlink=False, size=12) is None
    assert exclusions.admit_content(b"\x89PNG\r\n\x1a\nrest") is ExclusionClass.BINARY
    assert exclusions.admit_content(b"x = 1\x00\n") is ExclusionClass.BINARY
    assert exclusions.admit_content(b"x = 1\n") is None
    assert exclusions.counts == {**_ZERO, ExclusionClass.BINARY: 2}


def test_class_11_decode_or_parse_error() -> None:
    exclusions = _exclusions()
    facts = AstFacts(AstLanguage.PYTHON, "p", (), (), (), None, False, False)

    for failure in (AstFailureReason.SOURCE_DECODE_FAILED, AstFailureReason.AST_TIMEOUT):
        assert exclusions.admit_parse(failure) is ExclusionClass.PARSE_FAILED
    assert exclusions.admit_parse(facts) is None
    assert exclusions.counts == {**_ZERO, ExclusionClass.PARSE_FAILED: 2}


def test_path_class_of_an_import_target_counts_nothing() -> None:
    exclusions = _exclusions()

    assert exclusions.path_class("app/secrets.py") is ExclusionClass.SECRET
    assert exclusions.path_class("legacy/a.py") is ExclusionClass.IGNORED
    assert exclusions.path_class("app/link.py", symlink=True) is ExclusionClass.SYMLINK
    assert exclusions.path_class("app/models.py") is None
    assert exclusions.counts == _ZERO


def test_counters_add_up_over_a_build() -> None:
    exclusions = _exclusions()

    for path in ("vendor/a.go", "vendor/b.go", "README.md", "app/ok.py"):
        exclusions.admit_entry(path, symlink=False, size=1)

    assert exclusions.counts == {
        **_ZERO,
        ExclusionClass.VENDORED: 2,
        ExclusionClass.UNSUPPORTED_LANGUAGE: 1,
    }

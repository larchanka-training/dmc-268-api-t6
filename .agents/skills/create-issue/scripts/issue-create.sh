#!/usr/bin/env bash
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-create.sh
#
# issue-create.sh — create exactly one GitHub issue from a spec, idempotently,
# and put it on the course board.
#
# The sequence is dup-search → create once → read the issue back → board. It is
# written out as a sequence because the failure it guards against is specific and
# has happened: the create call returns nothing useful, the caller assumes
# failure, retries, and the server ends up with two issues — because the first
# call had in fact succeeded. So a create is never retried blind; a create that
# looks like it failed is followed by a lookup of the newest issues, and only an
# issue that did not exist before the create counts as "it landed".
#
# Usage: issue-create.sh <spec.json> [--dry-run] [--allow-duplicate] [--no-board]
# Output: {"url","number","repo","verified","board"} on stdout; board is
#         added | skipped | failed. --dry-run: {"dry_run","repo","body"} — it
#         still runs preflight, so it needs network and auth; it is not offline.
# Errors: {"error": "..."} on stderr (also {"warning": "..."}, exit unaffected).
#         stdout stays machine-readable.
# Exit: 0 created — check "verified", it can be false · 2 usage/spec/refused ·
#       3 auth · 4 4xx or duplicate blocked · 5 5xx/network, or a failed create
#       whose outcome could not be checked · 6 issue CREATED but the board step
#       failed — never re-run the create, only `gh project item-add` (stdout has
#       the url)
#
# Offline test seam: ISSUE_FIXTURE_DIR (see issue-preflight.sh). Additionally
# issue_search.json (duplicate guard), issue_recover_before.json and
# issue_recover.json (newest issues before / after a failed create),
# issue_create.json, issue_view.json and project_add.{code,err} stand in for the
# CLI calls; a missing file is a failed call. project_add.called appears when the
# board step ran.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="$HERE/issue-preflight.sh"
BUILDER="$HERE/build_issue_body.py"

# The course board: larchanka-training/projects/12 (dmc-268-t6).
BOARD_OWNER="larchanka-training"
BOARD_NUMBER="12"

json_str() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
die() { printf '{"error": %s}\n' "$(json_str "$1")" >&2; exit "${2:-2}"; }
warn() { printf '{"warning": %s}\n' "$(json_str "$1")" >&2; }

SPEC=""; DRY=false; ALLOW_DUP=false; NO_BOARD=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)         DRY=true; shift ;;
    --allow-duplicate) ALLOW_DUP=true; shift ;;
    --no-board)        NO_BOARD=true; shift ;;
    -h|--help)         sed -n '4,32p' "$0"; exit 0 ;;
    -*)                die "unknown argument: $1" 2 ;;
    *)                 SPEC="$1"; shift ;;
  esac
done
[ -n "$SPEC" ] && [ -f "$SPEC" ] \
  || die "usage: issue-create.sh <spec.json> [--dry-run] [--allow-duplicate] [--no-board]" 2

FIX="${ISSUE_FIXTURE_DIR:-}"

# ─── body and argv ──────────────────────────────────────────────────────────
# The emitter goes first: it is the one that knows what a valid spec is, so a bad
# file gets its message, not a generic one from here.
WORK="$(mktemp -d)" || die "cannot create a temp dir" 2
trap 'rm -rf "$WORK"' EXIT
# Deliberately mktemp, not "$TMPDIR/..." — TMPDIR is normally unset on Linux,
# so the obvious-looking version writes to / and fails confusingly.
BODY="$WORK/body.md"
python3 "$BUILDER" "$SPEC" > "$BODY" || die "spec rejected by build_issue_body.py (see stderr above)" 2
ARGS_JSON="$(python3 "$BUILDER" "$SPEC" --emit-args)" \
  || die "build_issue_body.py could not build the argument vector (see stderr above)" 2
# One argv element per line: the emitter rejects newlines in every value that lands here.
ARGV_TXT="$(printf '%s' "$ARGS_JSON" | python3 -c 'import json,sys; [print(a) for a in json.load(sys.stdin)]')" \
  || die "cannot read the argument vector" 2
mapfile -t ARGV <<< "$ARGV_TXT"

read_spec() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get(sys.argv[2]) or "")' "$SPEC" "$1"; }
REPO="$(read_spec repo)"
SUMMARY="$(read_spec summary)"
[ -n "$REPO" ] && [ -n "$SUMMARY" ] || die "spec must carry repo and summary" 2

# ─── attribution guard ──────────────────────────────────────────────────────
# Refuses to post what carries a sign of assistant authorship — in the body, the
# title or any argv value. Only authorship markers are blocked: a trailer line
# `Co-authored-by:`, "Generated with", the assistant's no-reply address and a
# session link. Prose that merely names a file or a tool (CLAUDE.md,
# .claude/skills, "Claude Code") is legitimate repository vocabulary and passes.
# Unconditional on purpose: a check cannot be entered with the wrong flag. It runs
# before the dry-run exit, so the preview step already catches a marker.
# A scratch file rather than a pipe: under `pipefail`, `grep -q` quitting on the
# first match can SIGPIPE the writer and turn a hit into a pass.
{ cat "$BODY"; printf '%s\n' "${ARGV[@]}"; } > "$WORK/guard.txt"
if grep -Eiq -e '^[[:space:]]*co-authored-by:' -e 'generated with' \
             -e 'noreply@anthropic\.com' -e 'claude\.ai/code/session' "$WORK/guard.txt"; then
  die "refusing to post: the composed issue carries an assistant-authorship marker (Co-authored-by trailer, 'Generated with', no-reply address or session link) — remove it from the spec" 2
fi

# ─── preflight ──────────────────────────────────────────────────────────────
# Re-run rather than trust the spec: auth and reachability can change between
# composing the issue and posting it, and this is the cheap place to find out.
PRE="$("$PREFLIGHT" --repo "$REPO" 2>&1)"; PRC=$?
[ $PRC -eq 0 ] || { printf '%s\n' "$PRE" >&2; exit $PRC; }

if [ "$DRY" = true ]; then
  python3 - "$REPO" "$BODY" <<'PY'
import json, sys
repo, body = sys.argv[1:3]
print(json.dumps({"dry_run": True, "repo": repo,
                  "body": open(body, encoding="utf-8").read()}, ensure_ascii=False))
PY
  exit 0
fi

# ─── 1. duplicate guard ─────────────────────────────────────────────────────
# Search is substring-ish, so compare the returned title for exact equality
# before believing it is the same issue. Fail-open by decision: when the search
# itself fails the guard warns and lets the create proceed.
if [ "$ALLOW_DUP" != true ]; then
  if [ -n "$FIX" ]; then
    HITS="$(cat "$FIX/issue_search.json" 2>/dev/null)"; SRC=$?
  else
    HITS="$(gh issue list --repo "$REPO" --state all --search "$SUMMARY" \
              --json number,title,url --limit 20 2>/dev/null)"; SRC=$?
  fi
  if [ $SRC -ne 0 ]; then
    warn "the duplicate search failed — continuing without the duplicate guard"
    HITS='[]'
  fi
  DUP="$(printf '%s' "$HITS" | python3 -c 'import json,sys
want=sys.argv[1]
try: rows=json.load(sys.stdin)
except Exception: rows=[]
for r in rows:
    if (r.get("title") or "").strip() == want.strip():
        print(json.dumps(r)); break' "$SUMMARY")"
  if [ -n "$DUP" ]; then
    die "an issue with exactly this title already exists in $REPO: $(printf '%s' "$DUP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("url") or d.get("number"))') — pass --allow-duplicate to create another anyway" 4
  fi
fi

# ─── 2. snapshot, then create exactly once ──────────────────────────────────
# The newest issues, newest first — a plain listing, not a search: the search
# index lags, so a fresh issue can be missing from it. Taken before the create so
# that recovery can tell an issue this run made from one that was already there
# (--allow-duplicate makes a same-title predecessor legitimate).
recent_issues() {  # $1: fixture file name
  if [ -n "$FIX" ]; then
    cat "$FIX/$1" 2>/dev/null
  else
    gh api "repos/$REPO/issues?state=all&sort=created&direction=desc&per_page=30" 2>/dev/null
  fi
}
BEFORE_OK=false
recent_issues issue_recover_before.json > "$WORK/before.json" && BEFORE_OK=true

if [ -n "$FIX" ]; then
  OUT="$(cat "$FIX/issue_create.json" 2>/dev/null)"; CRC=$?
  URL="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("url",""))
except Exception: print("")')"
else
  URL="$(gh issue create --repo "$REPO" --body-file "$BODY" "${ARGV[@]}" 2>"$WORK/err")"; CRC=$?
fi
URL="$(printf '%s' "$URL" | grep -Eo 'https?://[^[:space:]]+' | tail -1)"

# ─── 3. recover by looking, never by retrying ───────────────────────────────
if [ -z "$URL" ] || [ $CRC -ne 0 ]; then
  # The create may still have succeeded. Only an issue with this title that was
  # NOT in the snapshot counts; one that was already there is somebody else's.
  # exit 0 = found (url on stdout) · 1 = nothing new · 2 = a lookup was unusable
  FRC=2
  if [ "$BEFORE_OK" = true ] && recent_issues issue_recover.json > "$WORK/after.json"; then
    FOUND="$(python3 - "$SUMMARY" "$WORK/before.json" "$WORK/after.json" <<'PY'
import json, sys

want, before_path, after_path = sys.argv[1:4]


def load(path):
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("not a list of issues")
    return rows


def same(row):
    # pull requests share the issues endpoint; they are not what was created
    return "pull_request" not in row and (row.get("title") or "").strip() == want.strip()


try:
    before, after = load(before_path), load(after_path)
except Exception:
    sys.exit(2)
seen = {r.get("number") for r in before if same(r)}
for row in after:  # newest first
    if same(row) and row.get("number") not in seen and row.get("html_url"):
        print(row["html_url"])
        sys.exit(0)
sys.exit(1)
PY
)"; FRC=$?
  fi
  case $FRC in
    0)
      # It did land. Reporting it is right; retrying would have duplicated it.
      URL="$FOUND" ;;
    1)
      ERRTXT="$(cat "$WORK/err" 2>/dev/null | head -3)"
      case "$ERRTXT" in
        *401*|*403*|*authentication*|*Unauthorized*) die "create rejected: $ERRTXT" 3 ;;
        *5[0-9][0-9]*|*timeout*|*"connection refused"*) die "create failed against the server: $ERRTXT" 5 ;;
        *) die "create failed and no new issue with this title exists — not retrying. ${ERRTXT:-no error text}" 4 ;;
      esac ;;
    *)
      die "the create returned no issue url and the repository could not be checked for it — outcome unknown: look at the newest issues of $REPO by hand before retrying" 5 ;;
  esac
fi

NUMBER="$(printf '%s' "$URL" | grep -Eo '[0-9]+$')"

# ─── 4. verify by reading back ──────────────────────────────────────────────
# Success is the read-back, not the exit code of the create call. REST, not
# `gh issue view`: on gh 2.46 that command fails on the retired projectCards
# field.
if [ -n "$FIX" ]; then
  VIEW="$(cat "$FIX/issue_view.json" 2>/dev/null || echo '{}')"
else
  VIEW="$(gh api "repos/$REPO/issues/$NUMBER" 2>/dev/null || echo '{}')"
fi
VERIFIED="$(printf '%s' "$VIEW" | python3 -c 'import json,sys
want=sys.argv[1]
try: got=json.load(sys.stdin).get("title") or ""
except Exception: got=""
print("true" if got.strip() == want.strip() else "false")' "$SUMMARY")"

# ─── 5. board ───────────────────────────────────────────────────────────────
# After the create, so a missing token scope can never lose the issue: the issue
# exists, the url is on stdout, and the exit code (6) says only the board is
# left to do.
BOARD=skipped; BERR=""; BRC=0
if [ "$NO_BOARD" != true ]; then
  if [ -n "$FIX" ]; then
    : > "$FIX/project_add.called"
    BRC="$(cat "$FIX/project_add.code" 2>/dev/null || echo 0)"
    BERR="$(cat "$FIX/project_add.err" 2>/dev/null)"
  else
    BERR="$(gh project item-add "$BOARD_NUMBER" --owner "$BOARD_OWNER" --url "$URL" 2>&1 >/dev/null)"; BRC=$?
  fi
  if [ "$BRC" -eq 0 ]; then BOARD=added; else BOARD=failed; fi
fi

python3 - "$URL" "$NUMBER" "$REPO" "$VERIFIED" "$BOARD" <<'PY'
import json, sys
url, num, repo, verified, board = sys.argv[1:6]
print(json.dumps({"url": url, "number": int(num) if num else None,
                  "repo": repo, "verified": verified == "true",
                  "board": board}, ensure_ascii=False))
PY

if [ "$BOARD" = failed ]; then
  case "$(printf '%s' "$BERR" | tr '[:upper:]' '[:lower:]')" in
    *scope*) MSG="run gh auth refresh -s project" ;;
    *)       MSG="$(printf '%s' "$BERR" | head -3)" ;;
  esac
  MSG="${MSG:-gh gave no error text}"
  die "issue created ($URL) but not added to board $BOARD_NUMBER — $MSG. Do not re-run the create; retry only: gh project item-add $BOARD_NUMBER --owner $BOARD_OWNER --url $URL" 6
fi

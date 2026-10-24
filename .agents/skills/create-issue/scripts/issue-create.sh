#!/usr/bin/env bash
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-create.sh
#
# issue-create.sh — create exactly one GitHub issue from a spec, idempotently,
# put it on the course board with its Status, and set its native links.
#
# The sequence is validation → dup-search → create once → read the issue back →
# board and Status → links (issue-link.sh). Validation refuses, before anything exists, a milestone that is not open
# in the repo, a board Status that is not an option and a link target that is
# missing or a pull request. The rest is written out as a sequence because the failure it guards against is specific and
# has happened: the create call returns nothing useful, the caller assumes
# failure, retries, and the server ends up with two issues — because the first
# call had in fact succeeded. So a create is never retried blind; a create that
# looks like it failed is followed by a lookup of the newest issues, and only an
# issue that did not exist before the create AND was opened by the authenticated
# user counts as "it landed".
#
# Usage: issue-create.sh <spec.json> [--dry-run] [--allow-duplicate] [--no-board]
# Output: {"url","number","repo","verified","board","status"[,"links"]} on
#         stdout; board is added | skipped | failed, status the board Status read
#         back (null when skipped), links issue-link.sh's result (only when the
#         spec asks for links); verified also checks the milestone when one is set. --dry-run: {"dry_run","repo","body","milestone","status","links"}
#         — it still runs preflight and validation, so it needs network and auth;
#         it is not offline.
# Errors: {"error": "..."} on stderr (also {"warning": "..."}, exit unaffected).
#         stdout stays machine-readable.
# Exit: 0 created — check "verified", it can be false · 2 usage/spec/refused ·
#       3 auth · 4 4xx or duplicate blocked · 5 5xx/network, or a failed create
#       whose outcome could not be checked · 6 issue CREATED but the board step
#       (item-add or Status) failed — never re-run the create, only the gh command
#       in the message (stdout has the url) · 7 issue CREATED but a native link
#       failed — never re-run the create, only the issue-link.sh command in the
#       message; 7 wins over 6 when both fail, and stdout carries both
# Env: ISSUE_STATUS_RETRY_DELAY (seconds, default 3) — the pause before the one
#       re-edit of a Status the board's automation overwrote.
#
# Offline test seam: ISSUE_FIXTURE_DIR (see issue-preflight.sh). Additionally
# issue_search.json (duplicate guard), issue_recover_before.json and
# issue_recover.json (newest issues before / after a failed create),
# milestones.json, project_view.json, project_fields.json (validation; a missing
# file is a failed call whose stderr is <name>.err), issue_create.json,
# issue_view.json, project_add.{code,err,json}, project_edit.{code,err} and
# project_status.json / project_status.retry.json (the Status read after the edit
# and after the retry) stand in for the CLI calls; a missing file is a failed
# call. project_add.called appears when the board step ran; the validation calls,
# the create and the board calls are logged to calls.log. Links and their targets
# are read by issue-link.sh from its own REST-path files (see there).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="$HERE/issue-preflight.sh"
BUILDER="$HERE/build_issue_body.py"
LINKER="$HERE/issue-link.sh"

# The course board: larchanka-training/projects/12 (dmc-268-t6).
BOARD_OWNER="larchanka-training"
BOARD_NUMBER="12"

json_str() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
die() { printf '{"error": %s}\n' "$(json_str "$1")" >&2; exit "${2:-2}"; }
warn() { printf '{"warning": %s}\n' "$(json_str "$1")" >&2; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

SPEC=""; DRY=false; ALLOW_DUP=false; NO_BOARD=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)         DRY=true; shift ;;
    --allow-duplicate) ALLOW_DUP=true; shift ;;
    --no-board)        NO_BOARD=true; shift ;;
    -h|--help)         sed -n '4,47p' "$0"; exit 0 ;;
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
# Native links, as issue-link.sh arguments ("--parent" / "--blocked-by" then the
# resolved OWNER/REPO#N — never a bare "#N", which a shell reads as a comment).
LINKS_JSON="$(python3 "$BUILDER" "$SPEC" --emit-links)" \
  || die "build_issue_body.py could not resolve the links (see stderr above)" 2
LINK_TXT="$(printf '%s' "$LINKS_JSON" | python3 -c 'import json,sys
for link in json.load(sys.stdin):
    print("--parent" if link["kind"] == "parent" else "--blocked-by")
    print(link["repo"] + "#" + str(link["number"]))')" || die "cannot read the link list" 2
LINK_ARGS=()
[ -z "$LINK_TXT" ] || mapfile -t LINK_ARGS <<< "$LINK_TXT"

read_spec() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get(sys.argv[2]) or "")' "$SPEC" "$1"; }
REPO="$(read_spec repo)"
SUMMARY="$(read_spec summary)"
[ -n "$REPO" ] && [ -n "$SUMMARY" ] || die "spec must carry repo and summary" 2
MILESTONE="$(read_spec milestone)"
STATUS="$(read_spec status)"

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
# Who is filing: recovery only adopts an issue this account opened.
ME="$(printf '%s' "$PRE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("user", ""))' 2>/dev/null)"

# ─── validation ─────────────────────────────────────────────────────────────
# Everything that can be refused is refused here, while nothing exists — in
# --dry-run too: a milestone that is not open in the repo, a board Status that is
# not an option, a link target that is missing or is a pull request.
fixture() {  # fixture NAME — offline stand-in: FIX/NAME.json, or a failed call (stderr: NAME.err)
  printf '%s\n' "$1" >> "$FIX/calls.log"
  if [ ! -f "$FIX/$1.json" ]; then
    cat "$FIX/$1.err" > "$WORK/err" 2>/dev/null
    return 1
  fi
  cat "$FIX/$1.json"
}
errtext() { head -c 300 "$WORK/err" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//'; }
# The exit class of a failed call, from the HTTP status gh reports — not from any
# three digits in the text, which can be an issue number.
fail_code() {
  local status
  status="$(grep -Eo 'HTTP [0-9]{3}' "$WORK/err" 2>/dev/null | head -1)"
  case "${status#HTTP }" in
    404)     echo 2 ;;
    401|403) echo 3 ;;
    *)
      case "$(lower "$(cat "$WORK/err" 2>/dev/null)")" in
        *authentication*|*unauthorized*|*"bad credentials"*) echo 3 ;;
        *) echo 5 ;;  # 5xx, timeout, connection refused, or anything unrecognised
      esac ;;
  esac
}

if [ -n "$STATUS" ] && [ "$NO_BOARD" = true ]; then
  die "the spec sets \"status\" but --no-board skips the board — drop one of the two" 2
fi
[ "$NO_BOARD" = true ] || STATUS="${STATUS:-Todo}"

# Milestones are per repository and never created here: "Sprint 3" in ui and in
# api are two objects that share a title.
if [ -n "$MILESTONE" ]; then
  if [ -n "$FIX" ]; then
    MS="$(fixture milestones)"
  else
    MS="$(gh api "repos/$REPO/milestones?state=open&per_page=100" 2>"$WORK/err")"
  fi || die "cannot list the milestones of $REPO: $(errtext)" "$(fail_code)"
  MSG="$(printf '%s' "$MS" | python3 -c 'import json,sys
want, repo = sys.argv[1:3]
try: rows = json.load(sys.stdin)
except Exception: rows = None
if not isinstance(rows, list): sys.exit(2)
titles = [r["title"] for r in rows if isinstance(r, dict) and isinstance(r.get("title"), str)]
if want in titles: sys.exit(0)
print("milestone \"%s\" not found in %s (open: %s)" % (want, repo, ", ".join(titles) or "none"))
sys.exit(1)' "$MILESTONE" "$REPO")"
  case $? in
    0) ;;
    1) die "$MSG" 2 ;;
    *) die "cannot list the milestones of $REPO: the response is not a list" 5 ;;
  esac
fi

# The board Status: project, field and option ids are looked up by name — never
# hard-coded — once, before the create.
board_unreadable() {
  case "$(lower "$(cat "$WORK/err" 2>/dev/null)")" in
    *scope*) die "cannot read board $BOARD_NUMBER to set Status: the token lacks the project scope — run gh auth refresh -s project, or pass --no-board" 3 ;;
  esac
  die "cannot read board $BOARD_NUMBER to set Status: ${1:-$(errtext)}" "$(fail_code)"
}
if [ "$NO_BOARD" != true ]; then
  if [ -n "$FIX" ]; then
    fixture project_view > "$WORK/project.json"
  else
    gh project view "$BOARD_NUMBER" --owner "$BOARD_OWNER" --format json \
      > "$WORK/project.json" 2>"$WORK/err"
  fi || board_unreadable
  if [ -n "$FIX" ]; then
    fixture project_fields > "$WORK/fields.json"
  else
    # field-list returns 30 fields unless told otherwise
    gh project field-list "$BOARD_NUMBER" --owner "$BOARD_OWNER" --format json --limit 100 \
      > "$WORK/fields.json" 2>"$WORK/err"
  fi || board_unreadable
  IDS="$(python3 - "$STATUS" "$BOARD_NUMBER" "$WORK/project.json" "$WORK/fields.json" <<'PY'
import json, sys
want, board, project_path, fields_path = sys.argv[1:5]
try:
    with open(project_path, encoding="utf-8") as fh:
        project_id = json.load(fh)["id"]
    with open(fields_path, encoding="utf-8") as fh:
        fields = json.load(fh)["fields"]
    status = next((f for f in fields if isinstance(f, dict) and f.get("name") == "Status"), None)
except Exception:
    sys.exit(3)
options = status.get("options") if status else None
if not isinstance(options, list):
    print(f"board {board} has no single-select Status field")
    sys.exit(1)
names = [str(o.get("name")) for o in options if isinstance(o, dict)]
hit = next((o for o in options if isinstance(o, dict) and o.get("name") == want), None)
if hit is None:
    print(f'status "{want}" is not an option of Status on board {board} '
          f'(options: {", ".join(names)})')
    sys.exit(1)
print(project_id, status["id"], hit["id"])
PY
)"
  case $? in
    0) read -r PROJECT_ID FIELD_ID OPTION_ID <<< "$IDS" ;;
    1) die "$IDS" 2 ;;
    *) board_unreadable "the response is not a project or a field list" ;;
  esac
fi

# Link targets: they exist, are issues, a parent shares the owner — the lookups
# issue-link.sh makes before it links, made now so a bad target stops the run.
if [ ${#LINK_ARGS[@]} -gt 0 ]; then
  CHECK="$("$LINKER" "$REPO" --check "${LINK_ARGS[@]}" 2>&1 >/dev/null)"; CHRC=$?
  [ $CHRC -eq 0 ] || { printf '%s\n' "$CHECK" >&2; exit $CHRC; }
fi

if [ "$DRY" = true ]; then
  python3 - "$REPO" "$BODY" "$MILESTONE" "$STATUS" "$LINKS_JSON" <<'PY'
import json, sys
repo, body, milestone, status, links = sys.argv[1:6]
print(json.dumps({"dry_run": True, "repo": repo,
                  "body": open(body, encoding="utf-8").read(),
                  "milestone": milestone or None, "status": status or None,
                  "links": json.loads(links)}, ensure_ascii=False))
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
  printf 'issue_create\n' >> "$FIX/calls.log"
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
  # NOT in the snapshot AND was opened by the authenticated user counts; one that
  # was already there, or that a teammate opened in the same window, is not ours.
  # exit 0 = found (url on stdout) · 1 = nothing new · 2 = a lookup was unusable
  FRC=2
  if [ "$BEFORE_OK" = true ] && recent_issues issue_recover.json > "$WORK/after.json"; then
    FOUND="$(python3 - "$SUMMARY" "$WORK/before.json" "$WORK/after.json" "$ME" <<'PY'
import json, sys

want, before_path, after_path, me = sys.argv[1:5]


def load(path):
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("not a list of issues")
    return rows


def same(row):
    # pull requests share the issues endpoint; they are not what was created
    return "pull_request" not in row and (row.get("title") or "").strip() == want.strip()


def mine(row):
    # no author, or somebody else's: not adopted
    user = row.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return bool(me) and isinstance(login, str) and login.lower() == me.lower()


try:
    before, after = load(before_path), load(after_path)
except Exception:
    sys.exit(2)
seen = {r.get("number") for r in before if same(r)}
for row in after:  # newest first
    if same(row) and mine(row) and row.get("number") not in seen and row.get("html_url"):
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
# With a milestone in the spec, the issue read back must carry that milestone too.
VERIFIED="$(printf '%s' "$VIEW" | python3 -c 'import json,sys
want, milestone = sys.argv[1:3]
try: got = json.load(sys.stdin)
except Exception: got = {}
if not isinstance(got, dict): got = {}
ms = got.get("milestone")
ok = (got.get("title") or "").strip() == want.strip() and (
    not milestone or (isinstance(ms, dict) and ms.get("title") == milestone))
print("true" if ok else "false")' "$SUMMARY" "$MILESTONE")"

# ─── 5. board and Status ────────────────────────────────────────────────────
# After the create, so a failure here can never lose the issue: it exists, the
# url is on stdout, and exit 6 says only the board is left to do. The board's
# "Item added to project" automation can set Todo after our edit has landed, so
# the Status is read back, and set once more after a pause if it differs.
# shellcheck disable=SC2016  # $id is a GraphQL variable, not a shell one
STATUS_QUERY='query($id: ID!) { node(id: $id) { ... on ProjectV2Item { fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } }'
set_status() {  # set_status [retry] — one edit, then the read-back into STATUS_GOT
  if [ -n "$FIX" ]; then
    printf 'project_edit\n' >> "$FIX/calls.log"
    EERR="$(cat "$FIX/project_edit.err" 2>/dev/null)"
    READ="$(fixture "project_status${1:+.$1}")"
  else
    EERR="$(gh project item-edit --id "$ITEM" --project-id "$PROJECT_ID" --field-id "$FIELD_ID" \
              --single-select-option-id "$OPTION_ID" 2>&1 >/dev/null)"
    READ="$(gh api graphql -f query="$STATUS_QUERY" -f id="$ITEM" 2>"$WORK/err")"
  fi
  STATUS_GOT="$(printf '%s' "$READ" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["data"]["node"]["fieldValueByName"]["name"] or "")
except Exception: print("")')"
}
BOARD=skipped; BMSG=""; STATUS_GOT=""
if [ "$NO_BOARD" != true ]; then
  if [ -n "$FIX" ]; then
    : > "$FIX/project_add.called"
    printf 'project_add\n' >> "$FIX/calls.log"
    BRC="$(cat "$FIX/project_add.code" 2>/dev/null || echo 0)"
    BERR="$(cat "$FIX/project_add.err" 2>/dev/null)"
    ADDED="$(cat "$FIX/project_add.json" 2>/dev/null)"
  else
    ADDED="$(gh project item-add "$BOARD_NUMBER" --owner "$BOARD_OWNER" --url "$URL" --format json 2>"$WORK/err")"; BRC=$?
    BERR="$(cat "$WORK/err" 2>/dev/null)"
  fi
  ITEM="$(printf '%s' "$ADDED" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["id"])
except Exception: pass')"
  if [ "$BRC" -ne 0 ] || [ -z "$ITEM" ]; then
    BOARD=failed
    case "$(lower "$BERR")" in
      *scope*) MSG="run gh auth refresh -s project" ;;
      *)       MSG="$(printf '%s' "$BERR" | head -3)" ;;
    esac
    [ "$BRC" -ne 0 ] || MSG="item-add returned no item id"
    BMSG="issue created ($URL) but not added to board $BOARD_NUMBER — ${MSG:-gh gave no error text}. Do not re-run the create; retry only: gh project item-add $BOARD_NUMBER --owner $BOARD_OWNER --url $URL"
  else
    set_status
    if [ "$STATUS_GOT" != "$STATUS" ]; then
      sleep "${ISSUE_STATUS_RETRY_DELAY:-3}" 2>/dev/null
      set_status retry
    fi
    if [ "$STATUS_GOT" = "$STATUS" ]; then
      BOARD=added
    else
      BOARD=failed
      BMSG="issue created ($URL) and added to board $BOARD_NUMBER, but its Status reads back as \"$STATUS_GOT\", not \"$STATUS\"${EERR:+ (item-edit: $(printf '%s' "$EERR" | head -3))}. Do not re-run the create; set it on the board, or retry only: gh project item-edit --id $ITEM --project-id $PROJECT_ID --field-id $FIELD_ID --single-select-option-id $OPTION_ID"
    fi
  fi
fi

# ─── 6. native links ────────────────────────────────────────────────────────
# Only now: there is nothing to link until the issue exists. A failed link never
# undoes the issue — exit 7 names the issue-link.sh command that repairs it, and
# a rerun of that command leaves the links already in place alone.
LINKS_OUT=""; LRC=0
if [ ${#LINK_ARGS[@]} -gt 0 ]; then
  LINKS_OUT="$("$LINKER" "$REPO#$NUMBER" "${LINK_ARGS[@]}" 2>"$WORK/link_err")"; LRC=$?
fi

python3 - "$URL" "$NUMBER" "$REPO" "$VERIFIED" "$BOARD" "$STATUS_GOT" "$LINKS_JSON" \
          "$LINKS_OUT" "$WORK/link_err" <<'PY'
import json, sys
url, num, repo, verified, board, status, plan, linked, err_path = sys.argv[1:10]
out = {"url": url, "number": int(num) if num else None,
       "repo": repo, "verified": verified == "true",
       "board": board, "status": status or None}
plan = json.loads(plan)
if plan:
    try:
        out["links"] = json.loads(linked)["links"]
    except Exception:
        # issue-link.sh stopped before linking (a lookup failed after the create):
        # every link is reported as not in place, with its error — never dropped.
        try:
            with open(err_path, encoding="utf-8") as fh:
                error = json.loads(fh.read().strip().splitlines()[-1])["error"]
        except Exception:
            error = "issue-link.sh gave no result"
        row = lambda link: {"ref": f'{link["repo"]}#{link["number"]}', "ok": False,
                            "action": None, "error": error}
        out["links"] = {
            "parent": next((row(l) for l in plan if l["kind"] == "parent"), None),
            "blocked_by": [row(l) for l in plan if l["kind"] == "blocked_by"],
        }
print(json.dumps(out, ensure_ascii=False))
PY

if [ "$LRC" -ne 0 ]; then
  FAILED="$(printf '%s' "$LINKS_OUT" | python3 -c 'import json,sys
try:
    links = json.load(sys.stdin)["links"]
    print(sum(not x["ok"] for x in [links["parent"], *links["blocked_by"]] if x))
except Exception:
    print(sys.argv[1])' "$(( ${#LINK_ARGS[@]} / 2 ))")"
  if [ "$VERIFIED" = true ]; then WHAT="created and verified $URL"
  else WHAT="created $URL (the read-back did not match — look at it)"; fi
  # Both failed: the board message first, then exit 7 — stdout carries both results.
  [ "$BOARD" != failed ] || printf '{"error": %s}\n' "$(json_str "$BMSG")" >&2
  die "$WHAT, but $FAILED native link(s) failed — the issue stays, do not re-create it; repair: $LINKER $REPO#$NUMBER ${LINK_ARGS[*]}" 7
fi
[ "$BOARD" != failed ] || die "$BMSG" 6

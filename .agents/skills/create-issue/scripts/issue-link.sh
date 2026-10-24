#!/usr/bin/env bash
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-link.sh
#
# issue-link.sh — set GitHub native links on an EXISTING issue, idempotently: its
# parent (it becomes a sub-issue) and the issues it is blocked by. issue-create.sh
# runs it after the create; run it by hand to repair a link that failed — a rerun
# only adds what is missing and never replaces a parent.
#
# Why every link is read back: a POST that errored may still have landed, and one
# that returned 2xx may not show up. The read decides, as the read-back decides
# for the create. Why every lookup comes first: a target that does not exist, or
# is a pull request, is refused before anything is written.
#
# Usage: issue-link.sh OWNER/REPO#N [--parent REF] [--blocked-by REF]...
#        issue-link.sh OWNER/REPO --check [--parent REF] [--blocked-by REF]...
#   REF is "#M" (the issue's own repo) or "OWNER/REPO#M" — quote "#M" in a shell,
#   unquoted it starts a comment. --check looks the targets up for an issue not
#   yet created in OWNER/REPO and writes nothing.
# Output: {"issue","links":{"parent":{"ref","ok","action"[,"error"]}|null,
#         "blocked_by":[...]}} on stdout; action is linked | exists, null when ok
#         is false. --check: {"repo","targets":[{"kind","ref","id"}]}.
# Errors: {"error": "..."} on stderr.
# Exit: 0 every link in place · 2 usage, bad ref, issue or target not found,
#       target is a pull request, parent of another owner · 3 auth · 5 5xx,
#       network or unknown · 7 at least one link failed (stdout still printed;
#       rerun the same command to retry)
#
# Offline test seam: ISSUE_FIXTURE_DIR. A REST call reads the file named after
# its path, query string dropped: GET repos/O/R/issues/N → repos/O/R/issues/N.json
# (N.after.json for the read-back after a POST); a missing file is a failed call
# whose stderr is the .err file next to it. POST reads <path>.post.code and
# .post.err. Every call is appended to calls.log ("GET <path>",
# "POST <path> <field>=<value>").

set -uo pipefail

json_str() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
die() { printf '{"error": %s}\n' "$(json_str "$1")" >&2; exit "${2:-2}"; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

USAGE="usage: issue-link.sh OWNER/REPO#N [--parent REF] [--blocked-by REF]... | issue-link.sh OWNER/REPO --check [--parent REF] [--blocked-by REF]..."
NAME='[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'
RE_REPO="^($NAME)\$"
RE_FULL="^($NAME)#([1-9][0-9]*)\$"
RE_SHORT='^#([1-9][0-9]*)$'

ISSUE=""; CHECK=false; HAVE_PARENT=false; PARENT_ARG=""; BLOCKED_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --parent)
      [ $# -ge 2 ] || die "--parent needs a value" 2
      [ "$HAVE_PARENT" = false ] || die "--parent given twice — an issue has one parent" 2
      HAVE_PARENT=true; PARENT_ARG="$2"; shift 2 ;;
    --blocked-by)
      [ $# -ge 2 ] || die "--blocked-by needs a value" 2
      BLOCKED_ARGS+=("$2"); shift 2 ;;
    --check)   CHECK=true; shift ;;
    -h|--help) sed -n '4,33p' "$0"; exit 0 ;;
    -*)        die "unknown argument: $1" 2 ;;
    *)         [ -z "$ISSUE" ] || die "$USAGE" 2; ISSUE="$1"; shift ;;
  esac
done

dots_only() { [[ "${1%%/*}" =~ ^\.+$ ]] || [[ "${1#*/}" =~ ^\.+$ ]]; }

if [ "$CHECK" = true ]; then
  [[ "$ISSUE" =~ $RE_REPO ]] || die "$USAGE" 2
  ! dots_only "$ISSUE" || die "$USAGE" 2
  REPO="$ISSUE"; NUMBER=""
else
  [[ "$ISSUE" =~ $RE_FULL ]] || die "$USAGE" 2
  REPO="${BASH_REMATCH[1]}"; NUMBER="${BASH_REMATCH[2]}"
  ! dots_only "$REPO" || die "$USAGE" 2
fi
[ "$HAVE_PARENT" = true ] || [ ${#BLOCKED_ARGS[@]} -gt 0 ] \
  || die "nothing to link — pass --parent and/or --blocked-by" 2

# ─── refs ───────────────────────────────────────────────────────────────────
# Same grammar as build_issue_body.py: "#M" or "OWNER/REPO#M", M > 0, nothing
# looser. KINDS / REFS run in link order — the parent first, then the blockers.
resolve() {  # resolve REF → OWNER/REPO#M on stdout, or exit 1
  local repo num
  if [[ "$1" =~ $RE_SHORT ]]; then
    repo="$REPO"; num="${BASH_REMATCH[1]}"
  elif [[ "$1" =~ $RE_FULL ]]; then
    repo="${BASH_REMATCH[1]}"; num="${BASH_REMATCH[2]}"
    ! dots_only "$repo" || return 1
  else
    return 1
  fi
  printf '%s#%s\n' "$repo" "$num"
}
KINDS=(); REFS=()
if [ "$HAVE_PARENT" = true ]; then
  REF="$(resolve "$PARENT_ARG")" \
    || die "--parent is not an issue ref: '$PARENT_ARG' — use '#N' or 'OWNER/REPO#N' (N > 0)" 2
  # GitHub only accepts a sub-issue under a parent with the same owner.
  [ "$(lower "${REF%%/*}")" = "$(lower "${REPO%%/*}")" ] \
    || die "--parent $REF belongs to owner ${REF%%/*}, but a sub-issue must share its parent's owner (${REPO%%/*})" 2
  KINDS+=(parent); REFS+=("$REF")
fi
SEEN=" "
for ARG in "${BLOCKED_ARGS[@]}"; do
  REF="$(resolve "$ARG")" \
    || die "--blocked-by is not an issue ref: '$ARG' — use '#N' or 'OWNER/REPO#N' (N > 0)" 2
  KEY="$(lower "$REF")"
  case "$SEEN" in *" $KEY "*) die "--blocked-by lists $REF more than once" 2 ;; esac
  SEEN="$SEEN$KEY "
  KINDS+=(blocked_by); REFS+=("$REF")
done

# ─── the one door to GitHub ─────────────────────────────────────────────────
FIX="${ISSUE_FIXTURE_DIR:-}"
WORK="$(mktemp -d)" || die "cannot create a temp dir" 2
trap 'rm -rf "$WORK"' EXIT

api_get() {  # api_get PATH [after] — body on stdout, gh's stderr in $WORK/err
  local fx="${1%%\?*}${2:+.$2}"
  if [ -n "$FIX" ]; then
    printf 'GET %s\n' "$1" >> "$FIX/calls.log"
    if [ ! -f "$FIX/$fx.json" ]; then
      cat "$FIX/$fx.err" > "$WORK/err" 2>/dev/null
      return 1
    fi
    cat "$FIX/$fx.json"
  else
    gh api "$1" 2>"$WORK/err"
  fi
}
api_post() {  # api_post PATH FIELD=VALUE — the response is not used: the sub-issue POST returns the parent
  if [ -n "$FIX" ]; then
    printf 'POST %s %s\n' "$1" "$2" >> "$FIX/calls.log"
    cat "$FIX/$1.post.err" > "$WORK/err" 2>/dev/null
    return "$(cat "$FIX/$1.post.code" 2>/dev/null || echo 0)"
  else
    gh api -X POST "$1" -F "$2" >/dev/null 2>"$WORK/err"
  fi
}
errtext() { head -c 300 "$WORK/err" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//'; }
# The exit class of a failed lookup, from the HTTP status gh reports — not from
# any three digits in the text, which can be an issue number.
fail_code() {
  local status
  status="$(grep -Eo 'HTTP [0-9]{3}' "$WORK/err" 2>/dev/null | head -1)"
  case "${status#HTTP }" in
    404)     echo 2 ;;
    401|403) echo 3 ;;
    *)
      case "$(tr '[:upper:]' '[:lower:]' < "$WORK/err" 2>/dev/null)" in
        *authentication*|*unauthorized*|*"bad credentials"*) echo 3 ;;
        *) echo 5 ;;  # 5xx, timeout, connection refused, or anything unrecognised
      esac ;;
  esac
}
# fields FILE → "ID KIND PARENT": the internal id (what the link endpoints take,
# never the number), issue | pull_request, the parent's API url or "-".
fields() {
  python3 -c 'import json,sys
try: d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception: sys.exit(1)
if not isinstance(d, dict) or type(d.get("id")) is not int: sys.exit(1)
print(d["id"], "pull_request" if "pull_request" in d else "issue",
      d.get("parent_issue_url") or "-")' "$1"
}
lookup() {  # lookup OWNER/REPO#M → "ID KIND PARENT", or exit 2/3/5
  local rc
  if ! api_get "repos/${1%#*}/issues/${1##*#}" > "$WORK/issue.json"; then
    rc="$(fail_code)"
    [ "$rc" != 2 ] || die "$1 not found (or not visible to this account)" 2
    die "cannot read $1: $(errtext)" "$rc"
  fi
  fields "$WORK/issue.json" || die "cannot read $1: the response is not an issue" 5
}
# The issue an API url names, as OWNER/REPO#M.
ref_of() { local t="${1#*/repos/}"; printf '%s' "${t/\/issues\//#}"; }
# listed REF < issue list → 0 listed · 1 not listed · 2 not a list. Repository
# AND number: #7 of another repository is a different issue.
listed() {
  python3 -c 'import json,sys
repo, num = sys.argv[1].rsplit("#", 1)
try: rows = json.load(sys.stdin)
except Exception: sys.exit(2)
if not isinstance(rows, list): sys.exit(2)
for r in rows:
    if (isinstance(r, dict) and r.get("number") == int(num)
            and str(r.get("repository_url") or "").lower().endswith("/repos/" + repo.lower())):
        sys.exit(0)
sys.exit(1)' "$1"
}

# ─── 1. every lookup, before anything is written ────────────────────────────
if [ "$CHECK" = false ]; then
  # A die inside $(...) only ends the subshell: pass its exit code on.
  FOUND="$(lookup "$REPO#$NUMBER")" || exit $?
  read -r CHILD_ID CHILD_KIND CHILD_PARENT <<< "$FOUND"
  [ "$CHILD_KIND" = issue ] || die "$REPO#$NUMBER is a pull request, not an issue — it cannot be linked" 2
fi
IDS=()
for i in "${!REFS[@]}"; do
  FOUND="$(lookup "${REFS[i]}")" || exit $?
  read -r TID TKIND _ <<< "$FOUND"
  [ "$TKIND" = issue ] \
    || die "${REFS[i]} is a pull request, not an issue — it cannot be a parent or a blocker" 2
  IDS[i]="$TID"
done

if [ "$CHECK" = true ]; then
  python3 - "$REPO" "${KINDS[@]}" -- "${REFS[@]}" -- "${IDS[@]}" <<'PY'
import json, sys
repo, rest = sys.argv[1], sys.argv[2:]
cut = rest.index("--")
kinds, rest = rest[:cut], rest[cut + 1:]
cut = rest.index("--")
refs, ids = rest[:cut], rest[cut + 1:]
print(json.dumps({"repo": repo, "targets": [
    {"kind": k, "ref": r, "id": int(i)} for k, r, i in zip(kinds, refs, ids)]}))
PY
  exit 0
fi

BLOCKED_PATH="repos/$REPO/issues/$NUMBER/dependencies/blocked_by"
if [ ${#BLOCKED_ARGS[@]} -gt 0 ]; then
  if ! api_get "$BLOCKED_PATH?per_page=100" > "$WORK/blocked.json"; then
    die "cannot list what $REPO#$NUMBER is blocked by: $(errtext)" "$(fail_code)"
  fi
  python3 -c 'import json,sys; sys.exit(not isinstance(json.load(sys.stdin), list))' \
    < "$WORK/blocked.json" 2>/dev/null \
    || die "cannot list what $REPO#$NUMBER is blocked by: the response is not a list" 5
fi

# ─── 2. link, then read back ────────────────────────────────────────────────
RESULTS="$WORK/results.jsonl"; : > "$RESULTS"
result() {  # result KIND REF ACTION ERROR — an ERROR means the link is not in place
  python3 -c 'import json,sys
kind, ref, action, error = sys.argv[1:5]
row = {"kind": kind, "ref": ref, "ok": not error, "action": None if error else action}
if error:
    row["error"] = error
print(json.dumps(row, ensure_ascii=False))' "$@" >> "$RESULTS"
}

for i in "${!REFS[@]}"; do
  REF="${REFS[i]}"
  if [ "${KINDS[i]}" = parent ]; then
    if [ "$CHILD_PARENT" != "-" ]; then
      if [ "$(lower "$(ref_of "$CHILD_PARENT")")" = "$(lower "$REF")" ]; then
        result parent "$REF" exists ""
      else
        result parent "$REF" "" "$REPO#$NUMBER already has parent $(ref_of "$CHILD_PARENT"); never replaced — change it on GitHub if that is intended"
      fi
      continue
    fi
    api_post "repos/${REF%#*}/issues/${REF##*#}/sub_issues" "sub_issue_id=$CHILD_ID"; PRC=$?
    PERR="$(errtext)"
    # Read the child back: the POST answers with the parent, not the child.
    if api_get "repos/$REPO/issues/$NUMBER" after > "$WORK/back.json" \
       && BACK="$(fields "$WORK/back.json")"; then
      read -r _ _ BACK_PARENT <<< "$BACK"
      if [ "$BACK_PARENT" != "-" ] && [ "$(lower "$(ref_of "$BACK_PARENT")")" = "$(lower "$REF")" ]; then
        result parent "$REF" linked ""
      elif [ "$PRC" -ne 0 ]; then
        result parent "$REF" "" "link call failed: ${PERR:-gh gave no error text}"
      else
        result parent "$REF" "" "link call returned ok, but the parent is not set on read-back"
      fi
    else
      result parent "$REF" "" "the read-back of $REPO#$NUMBER failed${PERR:+ (link call: $PERR)}"
    fi
  else
    if listed "$REF" < "$WORK/blocked.json"; then
      result blocked_by "$REF" exists ""
      continue
    fi
    api_post "$BLOCKED_PATH" "issue_id=${IDS[i]}"; PRC=$?
    PERR="$(errtext)"
    if api_get "$BLOCKED_PATH?per_page=100" after > "$WORK/back.json"; then
      listed "$REF" < "$WORK/back.json"; LRC=$?
      [ $LRC -eq 2 ] || cp "$WORK/back.json" "$WORK/blocked.json"
    else
      LRC=3
    fi
    case "$LRC" in
      0) result blocked_by "$REF" linked "" ;;
      1|2)
        if [ "$PRC" -ne 0 ]; then
          result blocked_by "$REF" "" "link call failed: ${PERR:-gh gave no error text}"
        else
          result blocked_by "$REF" "" "link call returned ok, but $REF is not listed on read-back"
        fi ;;
      *) result blocked_by "$REF" "" "the read-back of what $REPO#$NUMBER is blocked by failed${PERR:+ (link call: $PERR)}" ;;
    esac
  fi
done

python3 - "$REPO#$NUMBER" "$RESULTS" <<'PY'
import json, sys
issue, path = sys.argv[1:3]
links = {"parent": None, "blocked_by": []}
with open(path, encoding="utf-8") as fh:
    for line in fh:
        row = json.loads(line)
        if row.pop("kind") == "parent":
            links["parent"] = row
        else:
            links["blocked_by"].append(row)
print(json.dumps({"issue": issue, "links": links}, ensure_ascii=False))
PY

FAILED="$(grep -c '"ok": false' "$RESULTS")"
[ "$FAILED" -eq 0 ] \
  || die "$FAILED of ${#REFS[@]} link(s) on $REPO#$NUMBER failed — \"links\" on stdout says which; fix the cause and rerun this command (links already in place are left alone)" 7

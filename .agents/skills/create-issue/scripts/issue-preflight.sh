#!/usr/bin/env bash
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-preflight.sh
#
# issue-preflight.sh — resolve the target GitHub repository from git remotes and
# prove we can actually file an issue in it, before anything is composed.
#
# Why the probes run in this order: one failing `gh` call cannot tell a network
# outage from a rejected token, and telling a user "your token is bad" when the
# network is down sends them to fix the wrong thing. So reachability is checked
# first, unauthenticated, and only a reachable host is handed to the auth probe.
#
# Usage: issue-preflight.sh [--repo [github.com/]OWNER/REPO] [--remote NAME]
# Output: {"repo","user","reachable","authed","can_write","nested_repos"} on stdout.
# Errors: {"error": "..."} on stderr.
# Exit: 0 ok · 2 usage/not-a-repo/not-github · 3 auth · 4 cannot file an issue
#       · 5 unreachable
#
# Offline test seam: set ISSUE_FIXTURE_DIR to a directory and no network call is
# made — reach.code/reach.http, auth_user.json(+.code) and repo_perms.json are
# read from it instead.

set -uo pipefail

GITHUB_HOST="github.com"

die() { printf '{"error": %s}\n' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" >&2; exit "${2:-2}"; }

REPO_ARG=""; REMOTE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   [ $# -ge 2 ] && [ -n "$2" ] || die "--repo needs a value" 2; REPO_ARG="$2"; shift 2 ;;
    --remote) [ $# -ge 2 ] && [ -n "$2" ] || die "--remote needs a value" 2; REMOTE_ARG="$2"; shift 2 ;;
    -h|--help) sed -n '4,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

FIX="${ISSUE_FIXTURE_DIR:-}"

# ─── host alias table ───────────────────────────────────────────────────────
# A genuine DNS alias with no derivable rule: GitHub's SSH-over-443 endpoint is a
# different name for the same service. A regex cannot know this, so it is data.
api_host_for() {
  case "$1" in
    ssh.github.com) echo "github.com" ;;
    *)              echo "$1" ;;
  esac
}

# ─── remote URL parsing ─────────────────────────────────────────────────────
# Four forms in the wild. Deliberately not `${url#https://*/}` — that mangles
# https://user:token@host/path. The host is what follows the LAST "@" of the
# authority (a password may itself contain "@"), so no credential fragment can end
# up in it. Nothing here, or anywhere in this script, ever prints a remote URL.
parse_remote() {  # $1=url -> "host<TAB>path"
  local url="$1" host path auth
  url="${url%.git}"
  case "$url" in
    git@*:*)      host="${url#git@}"; host="${host%%:*}"; path="${url#*:}" ;;
    ssh://*|git://*|http://*|https://*)
                  url="${url#*://}"
                  auth="${url%%/*}"; host="${auth##*@}"; host="${host%%:*}"
                  path="${url#*/}" ;;
    *) return 1 ;;
  esac
  # path == url means there was no "/" to cut at: a bare host, not a repository.
  [ -n "$host" ] && [ -n "$path" ] && [ "$path" != "$url" ] || return 1
  printf '%s\t%s\n' "$host" "$path"
}

# ─── resolve repo ───────────────────────────────────────────────────────────
REPO=""
if [ -n "$REPO_ARG" ]; then
  # `--repo` takes OWNER/REPO, optionally prefixed with the host name.
  REPO="${REPO_ARG#"$GITHUB_HOST"/}"
  [ "$(printf '%s' "$REPO" | tr -cd / | wc -c)" -eq 1 ] \
    || die "--repo '$REPO_ARG' is not OWNER/REPO — this skill files issues on $GITHUB_HOST only" 2
else
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "not inside a git repository — pass --repo OWNER/REPO to say where the issue goes" 2
  mapfile -t NAMES < <(git remote 2>/dev/null)
  [ "${#NAMES[@]}" -gt 0 ] \
    || die "this repository has no remotes — pass --repo OWNER/REPO" 2

  pick=""
  if [ -n "$REMOTE_ARG" ]; then
    pick="$REMOTE_ARG"
  elif printf '%s\n' "${NAMES[@]}" | grep -qx origin; then
    pick=origin
  elif [ "${#NAMES[@]}" -eq 1 ]; then
    pick="${NAMES[0]}"
  else
    die "several remotes (${NAMES[*]}) and no origin — pass --remote NAME" 2
  fi

  URL="$(git remote get-url "$pick" 2>/dev/null)" \
    || die "no such remote: $pick" 2
  # The URL may carry credentials: errors below name the remote, never its URL.
  IFS=$'\t' read -r REMOTE_HOST REPO < <(parse_remote "$URL") \
    || die "cannot parse the URL of remote '$pick' (expected https://, ssh://, git:// or git@host:path)" 2

  API_HOST="$(api_host_for "$REMOTE_HOST")"
  SHOWN_HOST="$REMOTE_HOST"
  [[ "$SHOWN_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || SHOWN_HOST="an unrecognised host"
  [ "$API_HOST" = "$GITHUB_HOST" ] \
    || die "remote '$pick' points at $SHOWN_HOST — this skill files issues on $GITHUB_HOST only (pass --repo OWNER/REPO for a $GITHUB_HOST repository)" 2

  # GitHub is always exactly owner/repo.
  OWNER="${REPO%%/*}"; NAME="${REPO#*/}"; NAME="${NAME%%/*}"
  REPO="$OWNER/$NAME"
fi

# Whatever the source, the name goes into API paths and argv: owner and name are
# plain GitHub names or the run stops here. A name cut out of a remote URL is not
# echoed (the URL may carry credentials); a --repo value is the caller's own.
if [ -n "$REPO_ARG" ]; then WHAT="--repo '$REPO_ARG'"; else WHAT="the repository path of remote '${pick:-}'"; fi
OWNER="${REPO%%/*}"; NAME="${REPO#*/}"
for part in "$OWNER" "$NAME"; do
  [[ "$part" =~ ^[A-Za-z0-9_.-]+$ ]] && [[ ! "$part" =~ ^\.+$ ]] \
    || die "$WHAT is not a valid OWNER/REPO name (letters, digits, '_', '.', '-' only)" 2
done

# ─── nested repositories ────────────────────────────────────────────────────
# Resolving from cwd is right syntactically and can still be wrong
# semantically: a repository whose subdirectories are themselves repositories
# (a lab repo with git-ignored projects/, a monorepo of checkouts) will happily
# resolve to the outer one while the issue belongs to a nested one. Preflight
# cannot tell which is meant, so it reports what it found and lets the preview
# put the choice in front of a human.
NESTED=""
if [ -z "$REPO_ARG" ] && [ -z "$FIX" ]; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
  if [ -n "$ROOT" ]; then
    NESTED="$(find "$ROOT" -maxdepth 3 -name .git -not -path "$ROOT/.git" \
                -not -path "*/node_modules/*" -printf '%h\n' 2>/dev/null \
              | sed "s|^$ROOT/||" | sort | head -20)"
  fi
fi

# ─── probe 1: reachability, unauthenticated ─────────────────────────────────
if [ -n "$FIX" ]; then
  RC="$(cat "$FIX/reach.code" 2>/dev/null || echo 0)"
  HTTP="$(cat "$FIX/reach.http" 2>/dev/null || echo 200)"
else
  HTTP="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "https://$GITHUB_HOST/" 2>/dev/null)"; RC=$?
fi

if [ "$RC" -ne 0 ] || [ "$HTTP" = "000" ]; then
  case "$RC" in
    35|60|77|91) die "TLS failure talking to $GITHUB_HOST (curl $RC). This is a certificate or proxy problem — not auth, and not plain connectivity." 5 ;;
    *)           die "$GITHUB_HOST is unreachable (curl $RC, http $HTTP). This is a network problem — check connectivity and any proxy — not an auth problem." 5 ;;
  esac
fi

# ─── probe 2: authentication ────────────────────────────────────────────────
if [ -n "$FIX" ]; then
  ARC="$(cat "$FIX/auth_user.code" 2>/dev/null || echo 0)"
  USER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("login", ""))' "$FIX/auth_user.json" 2>/dev/null)"
else
  USER="$(gh api user --jq .login 2>/dev/null)"; ARC=$?
fi
if [ "$ARC" -ne 0 ] || [ -z "${USER:-}" ]; then
  die "not authenticated to $GITHUB_HOST — run: gh auth login" 3
fi

# ─── probe 3: can an issue be filed here ────────────────────────────────────
# Deliberately NOT a push-permission check. Filing an issue is not pushing: on
# a public repository any authenticated user may open one, so gating on
# `permissions.push` would refuse the ordinary case of reporting something
# upstream from a fork. What actually has to hold is that the repository is
# visible to us (the GET succeeding proves that) and that its issue tracker is
# switched on.
if [ -n "$FIX" ]; then
  CAN_WRITE="$(cat "$FIX/repo_perms.json" 2>/dev/null || echo true)"
else
  CAN_WRITE="$(gh api "repos/$REPO" --jq '.has_issues' 2>/dev/null)"
  [ -n "$CAN_WRITE" ] || CAN_WRITE=false     # GET failed → not visible to us
fi
if [ "$CAN_WRITE" != "true" ]; then
  die "cannot file an issue in $REPO as $USER — either the repository is not visible to this account or its issue tracker is disabled" 4
fi

# ─── result ─────────────────────────────────────────────────────────────────
python3 - "$REPO" "$USER" "$NESTED" <<'PY'
import json, sys
repo, user, nested = sys.argv[1:4]
print(json.dumps({
    "repo": repo, "user": user,
    "reachable": True, "authed": True, "can_write": True,
    "nested_repos": [p for p in nested.split("\n") if p.strip()],
}, ensure_ascii=False))
PY

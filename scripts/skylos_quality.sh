#!/usr/bin/env bash
# Skylos quality gate (#263): the CI job skylos-quality runs this script, and the same command
# works locally: git fetch origin && scripts/skylos_quality.sh origin/main
#
# Skylos --gate passes against BASE_REF, all run; the worst exit code wins:
#   1. new findings on the changed lines: dead code, quality, security, secrets;
#   2. size and complexity of the changed functions, compared with the merge base, so that a
#      function grown past a limit fails even when its `def` line did not change. Skylos compares
#      a function with the file of the same path at the merge base: a moved file has none, and
#      Skylos's own verdict would fail a moved legacy file. The verdict is therefore
#      scripts/skylos_changed_functions.py's: only metrics the change made worse;
#   3. pass 2 again for the Python files moved with edits, in a scratch repository whose base
#      holds each one's merge-base version at its new path: a function a move made worse fails,
#      as one changed in place. Pure moves (100% similar) need no pass.
# Limits and exclusions are [tool.skylos] in pyproject.toml. Skylos reads them from the merge base
# of BASE_REF and HEAD, so a change to them applies only to pull requests after its merge.
# Exit codes: 0 every gate passed, 1 a gate failed, 2 usage error or incomplete analysis.
set -euo pipefail

# The one pin of Skylos: CI and local runs both come through this script.
SKYLOS_VERSION=5.4.0
# Skylos's own dependencies resolve as uploaded before this time (uvx --exclude-newer), so that a
# new release of one of them cannot change the gate. The day after SKYLOS_VERSION's release:
# bump both together.
SKYLOS_RESOLVED_BEFORE=2026-11-03T00:00:00Z
# Function metrics a diff scan lists as advice; [tool.skylos.gate] rules makes them count.
FUNCTION_RULES=SKY-Q301,SKY-C303,SKY-C304

if [[ $# -lt 1 || $# -gt 2 || -z "$1" ]]; then
  echo "usage: scripts/skylos_quality.sh BASE_REF [REPORT_DIR]" >&2
  exit 2
fi
base="$1"
reports="${2:-$(mktemp -d)}"
mkdir -p "${reports}"
reports="$(cd "${reports}" && pwd)"
# Written only when files moved: a report of an earlier run must not pass for this one's.
rm -f "${reports}/moved-functions.json"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
repo="$(pwd)"

# skylos_gate <report name> <skylos args>: one scan with a JSON report. --gate also makes
# Skylos read the policy from the merge base.
skylos_gate() {
  local report="$1"
  shift
  uvx --exclude-newer "${SKYLOS_RESOLVED_BEFORE}" --from "skylos==${SKYLOS_VERSION}" \
    skylos . "$@" --gate --format json-ci -o "${reports}/${report}.json"
}

# git in the scratch repository of pass 3: no inherited GIT_* variable (a hook's GIT_DIR, the
# config of `git -c`), no user or system config (hooks, signing) and an identity of its own,
# which a CI runner lacks; useConfigOnly keeps git from guessing one instead.
scratch_git() {
  local name
  local unset=()
  for name in $(compgen -e); do
    if [[ "${name}" == GIT_* ]]; then
      unset+=(-u "${name}")
    fi
  done
  env "${unset[@]}" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_AUTHOR_NAME=skylos-quality GIT_AUTHOR_EMAIL=skylos-quality@localhost \
    GIT_COMMITTER_NAME=skylos-quality GIT_COMMITTER_EMAIL=skylos-quality@localhost \
    git -C "${scratch}" -c user.useConfigOnly=true "$@"
}

# The ERR trap of the scratch repository's setup in pass 3: a repository that cannot be built
# is an incomplete analysis (2), not a failed gate (1). A command substitution inherits the trap
# and leaves the message to the main shell.
# shellcheck disable=SC2329  # invoked by the ERR trap below, not by name
scratch_setup_failed() {
  if ((BASH_SUBSHELL == 0)); then
    echo "Pass 3: cannot build the scratch repository" >&2
    echo "Reports: ${reports}"
  fi
  exit 2
}

echo "== Skylos ${SKYLOS_VERSION}: new findings on the lines changed since ${base}"
status=0
skylos_gate changed-lines --summary --quality --danger --secrets \
  --diff-base "${base}" --diff "${base}" || status=$?

echo "== Skylos ${SKYLOS_VERSION}: size and complexity of the functions changed since ${base}"
functions=0
# Skylos's forced verdict would list moved files too: its output is shown only on a failure.
output="$(skylos_gate changed-functions --force --quality --diff-base "${base}" \
  --select "${FUNCTION_RULES}" 2>&1)" || functions=$?
if ((functions == 0)); then
  # The project's Python through uv, as every tool here: not whatever python3 is on PATH.
  uv run --no-project --python 3.13 scripts/skylos_changed_functions.py \
    "${reports}/changed-functions.json" "${FUNCTION_RULES}" || functions=$?
else
  printf '%s\n' "${output}" >&2
fi

echo "== Skylos ${SKYLOS_VERSION}: functions of the Python files moved with edits since ${base}"
moved=0
if merge_base="$(git merge-base "${base}" HEAD)"; then
  # errtrace: the trap also catches a failure inside scratch_git, which set -e alone would end
  # with git's own exit code.
  set -o errtrace
  trap scratch_setup_failed ERR
  work="$(mktemp -d)"
  trap 'rm -rf "${work}"' EXIT
  scratch="${work}/repo"
  mkdir "${scratch}"
  # Renames against the working tree, which Skylos scans; a record is status, old path, new path.
  git diff -M -z --name-status --diff-filter=R "${merge_base}" -- '*.py' >"${work}/renames"
  moved_files=()
  while IFS= read -r -d '' similarity && IFS= read -r -d '' old && IFS= read -r -d '' new; do
    if [[ "${similarity}" != R100 ]]; then
      mkdir -p "${scratch}/$(dirname "${new}")"
      git show "${merge_base}:${old}" >"${scratch}/${new}"
      moved_files+=("${new}")
    fi
  done <"${work}/renames"
  if ((${#moved_files[@]} > 0)); then
    # The merge base's policy, as in passes 1 and 2.
    if git cat-file -e "${merge_base}:pyproject.toml" 2>/dev/null; then
      git show "${merge_base}:pyproject.toml" >"${scratch}/pyproject.toml"
    fi
    scratch_git init -q -b scratch
    scratch_git add -A
    scratch_git commit -q -m "merge base: the moved files at their new paths"
    scratch_base="$(scratch_git rev-parse HEAD)"
    for file in "${moved_files[@]}"; do
      cp "${file}" "${scratch}/${file}"
    done
    scratch_git commit -q -a --allow-empty -m "HEAD"
  fi
  trap - ERR
  set +o errtrace
  echo "Moved with edits: ${#moved_files[@]}"
  if ((${#moved_files[@]} > 0)); then
    output="$(cd "${scratch}" && skylos_gate moved-functions --force --quality \
      --diff-base "${scratch_base}" --select "${FUNCTION_RULES}" 2>&1)" || moved=$?
    if ((moved == 0)); then
      # From the scratch repository, so that the listed paths are relative to it.
      (cd "${scratch}" && uv run --no-project --python 3.13 \
        "${repo}/scripts/skylos_changed_functions.py" "${reports}/moved-functions.json" \
        "${FUNCTION_RULES}" "Moved functions") || moved=$?
    else
      printf '%s\n' "${output}" >&2
    fi
  fi
else
  echo "No merge base of ${base} and HEAD: moved files are not checked" >&2
  moved=2
fi

for code in "${functions}" "${moved}"; do
  if ((code > status)); then
    status="${code}"
  fi
done
echo "Reports: ${reports}"
exit "${status}"

#!/usr/bin/env bash
# Skylos quality gate (#263): the CI job skylos-quality runs this script, and the same command
# works locally: git fetch origin && scripts/skylos_quality.sh origin/main
#
# Two Skylos --gate passes against BASE_REF, both run; the worse exit code wins:
#   1. new findings on the changed lines: dead code, quality, security, secrets;
#   2. size and complexity of the changed functions, compared with the merge base, so that a
#      function grown past a limit fails even when its `def` line did not change.
# Limits and exclusions are [tool.skylos] in pyproject.toml. Skylos reads them from the merge base
# of BASE_REF and HEAD, so a change to them applies only to pull requests after its merge.
# Exit codes: 0 both gates passed, 1 a gate failed, 2 usage error or incomplete analysis.
set -euo pipefail

# The one pin of Skylos: CI and local runs both come through this script.
SKYLOS_VERSION=5.4.0
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
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# skylos_gate <report name> <skylos args>: one gated scan with a JSON report.
skylos_gate() {
  local report="$1"
  shift
  uvx --from "skylos==${SKYLOS_VERSION}" skylos . "$@" --gate --summary \
    --format json-ci -o "${reports}/${report}.json"
}

echo "== Skylos ${SKYLOS_VERSION}: new findings on the lines changed since ${base}"
status=0
skylos_gate changed-lines --quality --danger --secrets --diff-base "${base}" --diff "${base}" \
  || status=$?

echo "== Skylos ${SKYLOS_VERSION}: size and complexity of the functions changed since ${base}"
functions=0
skylos_gate changed-functions --quality --diff-base "${base}" --select "${FUNCTION_RULES}" \
  || functions=$?

if ((functions > status)); then
  status="${functions}"
fi
echo "Reports: ${reports}"
exit "${status}"

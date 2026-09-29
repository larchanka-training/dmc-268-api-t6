import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const fixture = dirname(fileURLToPath(import.meta.url));
const temporary = mkdtempSync(join(tmpdir(), "log-04-oracle-"));
const source = "src/features/reviews/model/useReviewRun.tsx";

async function loadTransition(file) {
  const sourceText = readFileSync(file, "utf8")
    .replace(/^import \{ useReducer, type Dispatch \} from "react";\n/m, "");
  const javascript = stripTypeScriptTypes(sourceText);
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`;
  return (await import(moduleUrl)).transitionReviewRun;
}

function observe(transition) {
  const queued = { runId: "run-204", phase: "queued" };
  const started = transition(queued, { type: "started", runId: "run-204" });
  const cancelled = transition(started, { type: "cancelled", runId: "run-204" });
  const lateCompletion = transition(cancelled, { type: "completed", runId: "run-204" });
  const normalCompletion = transition(started, { type: "completed", runId: "run-204" });
  const unrelatedCompletion = transition(started, { type: "completed", runId: "run-205" });
  return {
    cancelledPhase: cancelled.phase,
    lateCompletionPhase: lateCompletion.phase,
    normalCompletionPhase: normalCompletion.phase,
    unrelatedCompletionPhase: unrelatedCompletion.phase,
  };
}

try {
  cpSync(join(fixture, "base"), temporary, { recursive: true });
  const file = join(temporary, source);
  const before = observe(await loadTransition(file));
  assert.deepEqual(before, {
    cancelledPhase: "cancelled",
    lateCompletionPhase: "cancelled",
    normalCompletionPhase: "passed",
    unrelatedCompletionPhase: "running",
  });

  execFileSync("git", ["apply", join(fixture, "diff.patch")], { cwd: temporary });
  const after = observe(await loadTransition(file));
  assert.deepEqual(after, {
    cancelledPhase: "cancelled",
    lateCompletionPhase: "passed",
    normalCompletionPhase: "passed",
    unrelatedCompletionPhase: "running",
  });
  console.log("late completion changes cancelled run to passed only after the patch");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

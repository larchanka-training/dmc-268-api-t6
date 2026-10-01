import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const caseDirectory = dirname(fileURLToPath(import.meta.url));
const temporary = mkdtempSync(join(tmpdir(), "clean-04-oracle-"));
const source = join(temporary, "src/review/check_summary.ts");
const inputs = [
  { passed: 0, failed: 0, skipped: 0 },
  { passed: 3, failed: 0, skipped: 1 },
  { passed: 0, failed: 5, skipped: 2 },
  { passed: 7, failed: 2, skipped: 0 },
  { passed: -1, failed: 2, skipped: 0 },
  { passed: 1.5, failed: 0.5, skipped: 1 },
  { passed: 1_000_000, failed: 1, skipped: 0 },
].map(Object.freeze);

async function load(file) {
  const javascript = stripTypeScriptTypes(readFileSync(file, "utf8"));
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`;
  return import(moduleUrl);
}

try {
  cpSync(join(caseDirectory, "base"), temporary, { recursive: true });
  const beforeModule = await load(source);
  const beforeFunction = beforeModule.formatCheckSummary;
  const before = inputs.map(beforeFunction);
  assert.deepEqual(before.slice(0, 4), [
    "0/0 checks passed (0 skipped)",
    "3/4 checks passed (1 skipped)",
    "0/7 checks passed (2 skipped)",
    "7/9 checks passed (0 skipped)",
  ]);

  execFileSync("git", ["apply", join(caseDirectory, "diff.patch")], { cwd: temporary });
  const afterModule = await load(source);
  const afterFunction = afterModule.formatCheckSummary;
  const after = inputs.map(afterFunction);
  assert.equal(afterFunction.toString(), beforeFunction.toString());
  assert.deepEqual(after, before);
  console.log("same emitted function and seven summary outputs after type-only change");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

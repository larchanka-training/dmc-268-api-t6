import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const fixture = dirname(fileURLToPath(import.meta.url));
const temporary = mkdtempSync(join(tmpdir(), "syn-02-oracle-"));
const source = "src/components/RequiredChecks.tsx";

function renderedChecks(text) {
  const values = text.match(/const CHECK_NAMES = \[([^\]]+)\] as const;/);
  if (values) {
    assert.match(text, /CHECK_NAMES\.map\(\(name\) => \(/);
    assert.match(text, /<li key=\{name\} className="required-check">\s*<span className="required-check__name">\{name\}<\/span>\s*<\/li>/);
    return [...values[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  }

  const items = [...text.matchAll(/<li className="required-check">\s*<span className="required-check__name">([^<]+)<\/span>\s*<\/li>/g)];
  return items.map((match) => match[1]);
}

try {
  cpSync(join(fixture, "base"), temporary, { recursive: true });
  const file = join(temporary, source);
  const before = renderedChecks(readFileSync(file, "utf8"));
  assert.deepEqual(before, ["lint", "typecheck", "test"]);
  execFileSync("git", ["apply", join(fixture, "diff.patch")], { cwd: temporary });
  const after = renderedChecks(readFileSync(file, "utf8"));
  assert.deepEqual(after, before);
  console.log("same three checks and list item markup before and after patch");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

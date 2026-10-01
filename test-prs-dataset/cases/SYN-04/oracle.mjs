import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const caseDirectory = dirname(fileURLToPath(import.meta.url));
const temporary = mkdtempSync(join(tmpdir(), "syn-04-oracle-"));
const source = join(temporary, ".upstream/src/middleware/devtools.ts");

function augmentations() {
  const text = readFileSync(source, "utf8");
  return [...text.matchAll(/^declare module '\.\.\/vanilla' \{\n[\s\S]*?^\}/gm)].map(
    (match) => match[0],
  );
}

try {
  cpSync(join(caseDirectory, "base"), temporary, { recursive: true });
  const before = augmentations();
  assert.equal(before.length, 1);
  execFileSync("git", ["apply", "--directory=.upstream", join(caseDirectory, "diff.patch")], {
    cwd: temporary,
  });
  const after = augmentations();
  assert.equal(after.length, 2);
  assert.equal(after[0], before[0]);
  assert.equal(after[1], before[0]);
  assert.match(after[1], /'zustand\/devtools': WithDevtools<S>/);
  console.log("identical StoreMutators augmentation blocks: 1 before, 2 after");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

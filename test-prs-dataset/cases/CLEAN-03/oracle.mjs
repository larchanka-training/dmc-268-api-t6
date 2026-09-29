import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const caseDirectory = dirname(fileURLToPath(import.meta.url));
const temporary = mkdtempSync(join(tmpdir(), "clean-03-oracle-"));
const source = join(temporary, ".upstream/src");

function read(path) {
  return readFileSync(join(source, path), "utf8");
}

function gitBlob(path) {
  const contents = readFileSync(path);
  return createHash("sha1")
    .update(`blob ${contents.length}\0`)
    .update(contents)
    .digest("hex");
}

try {
  cpSync(join(caseDirectory, "base"), temporary, { recursive: true });
  const beforeIndex = read("index.ts");
  const beforeVanilla = read("vanilla.ts");
  const beforeLicense = readFileSync(join(temporary, ".upstream/LICENSE"));
  assert.equal(gitBlob(join(source, "index.ts")), "9f17bee494c83d942ad16b8df9261e00538f944b");
  assert.equal(gitBlob(join(source, "vanilla.ts")), "b1e38ff2bcf0dcbf3927263006803db2a077c085");
  assert.equal(
    gitBlob(join(temporary, ".upstream/LICENSE")),
    "a2c2649deec2aabf248294a60a6e2e63a58f4a2b",
  );
  assert.equal(existsSync(join(source, "react.ts")), false);
  assert.match(beforeIndex, /useReducer/);
  assert.match(beforeIndex, /export default create\n$/);
  assert.match(beforeVanilla, /export default create\n$/);

  execFileSync(
    "git",
    ["apply", "--directory=.upstream", join(caseDirectory, "diff.patch")],
    { cwd: temporary },
  );

  const afterIndex = read("index.ts");
  const afterReact = read("react.ts");
  const afterVanilla = read("vanilla.ts");
  assert.equal(gitBlob(join(source, "index.ts")), "b4d7737d2bb237dabbff2d078a12821e119b6c69");
  assert.equal(gitBlob(join(source, "react.ts")), "bf7b6ba6e941dd372b6a59c889dbc2d6596d527d");
  assert.equal(gitBlob(join(source, "vanilla.ts")), "027a66f55301a412bc460c2f76e1feba423bade9");
  const movedReact = beforeIndex
    .replace("export * from './vanilla'\n", "")
    .replaceAll("createImpl", "createStore");
  assert.equal(afterReact, movedReact);
  assert.equal(
    afterIndex,
    "export * from './vanilla'\nexport * from './react'\nexport { default } from './react'\n",
  );
  assert.equal(
    afterVanilla,
    beforeVanilla
      .replaceAll("function create<", "function createStore<")
      .replace("export default create\n", "export default createStore\n"),
  );
  assert.deepEqual(readFileSync(join(temporary, ".upstream/LICENSE")), beforeLicense);
  console.log("React hook body retained; exports and source Git blobs match upstream");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

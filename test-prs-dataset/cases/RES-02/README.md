# RES-02 oracle

The pre-image passes the same `onMessage` callback to `subscribe` and `unsubscribe`, so unmount removes the listener. The patch subscribes a new wrapper but still unsubscribes `onMessage`. Because listener identity differs, the unmount cleanup removes nothing: the source retains the wrapper, and later notifications still invoke the stale callback. Repeated mount/unmount cycles accumulate listeners and duplicate deliveries. The ground truth is one `performance` / `medium` finding on the added `source.subscribe(listener)` line 14, yielding `attention`. This demonstrates a growing subscription leak without asserting an immediate critical outage.

This dependency-free oracle executes the actual pre-image and patched hook bodies using Node's built-in TypeScript stripping, a fake `useEffect`, and an in-memory event source. Run from the repository root with Node 22:

```sh
node --input-type=module <<'JS'
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { stripTypeScriptTypes } from "node:module";
import { runInNewContext } from "node:vm";

const fixture = resolve("test-prs-dataset/cases/RES-02");
const temporary = mkdtempSync(join(tmpdir(), "res-02-oracle-"));

function observe(file) {
  const listeners = new Set();
  const source = {
    subscribe(listener) { listeners.add(listener); },
    unsubscribe(listener) { listeners.delete(listener); },
    emit(message) { for (const listener of listeners) listener(message); },
  };
  let cleanup;
  let deliveries = 0;
  const code = stripTypeScriptTypes(readFileSync(file, "utf8"))
    .replace('import { useEffect } from "react";', "")
    .replace("export function useNotifications", "function useNotifications");
  runInNewContext(`${code}\nuseNotifications(source, onMessage);`, {
    source,
    onMessage: () => { deliveries += 1; },
    useEffect(effect) { cleanup = effect(); },
  });
  assert.equal(listeners.size, 1);
  assert.equal(typeof cleanup, "function");
  cleanup();
  source.emit("after unmount");
  return { remaining: listeners.size, deliveries };
}

try {
  cpSync(join(fixture, "base"), temporary, { recursive: true });
  const file = join(temporary, "src/useNotifications.tsx");
  assert.deepEqual(observe(file), { remaining: 0, deliveries: 0 });
  execFileSync("git", ["apply", join(fixture, "diff.patch")], { cwd: temporary });
  assert.deepEqual(observe(file), { remaining: 1, deliveries: 1 });
  console.log("pre-image cleans up; patched hook retains one listener after unmount");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
JS
```

The source is synthetic. The code and patch contain no answer-hint comments.

# RES-04 oracle

`SocketMessageTracker` attaches separate `message` and `close` listeners to an `EventTarget`. The pre-image removes both in `dispose()`. The patch changes the first removal to target `close` while still passing the `message` callback. No matching `close` listener exists for that callback, so the `message` listener remains attached after disposal. Repeated tracker lifecycles retain callbacks and their tracker instances on a long-lived socket; messages are still delivered to disposed trackers.

Ground truth is one `performance` / `medium` finding on the added `removeEventListener` call at line 21, yielding `attention`. Three disposed trackers leave three listeners in the oracle below. This proves retention and stale callback delivery, without assuming a service-wide outage. The source is synthetic TypeScript and contains no answer-hint comments. This explicit `dispose()` lifecycle is separate from the React effect lifecycle in RES-02.

Run the dependency-free oracle from the repository root with Node 22. It executes the actual TypeScript pre-image and patched file after built-in type stripping:

```sh
node --input-type=module <<'JS'
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { getEventListeners } from "node:events";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const fixture = resolve("test-prs-dataset/cases/RES-04");
const temporary = mkdtempSync(join(tmpdir(), "res-04-oracle-"));

async function observe(file) {
  const javascript = stripTypeScriptTypes(readFileSync(file, "utf8"));
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`;
  const { SocketMessageTracker } = await import(moduleUrl);
  const socket = new EventTarget();
  const trackers = Array.from({ length: 3 }, () => new SocketMessageTracker(socket));
  for (const tracker of trackers) tracker.dispose();
  const remaining = getEventListeners(socket, "message").length;
  socket.dispatchEvent(new Event("message"));
  return { remaining, deliveries: trackers.map((tracker) => tracker.messageCount) };
}

try {
  cpSync(join(fixture, "base"), temporary, { recursive: true });
  const file = join(temporary, "src/socket-message-tracker.ts");
  assert.deepEqual(await observe(file), { remaining: 0, deliveries: [0, 0, 0] });
  execFileSync("git", ["apply", join(fixture, "diff.patch")], { cwd: temporary });
  assert.deepEqual(await observe(file), { remaining: 3, deliveries: [1, 1, 1] });
  console.log("disposed trackers: 0 listeners before, 3 listeners after");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
JS
```

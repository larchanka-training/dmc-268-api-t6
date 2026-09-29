# LOG-02 oracle

The checkout settlement decision is the input to an order fulfillment queue.
`releaseShipment: true` releases a paid order; a gateway `pending` result must
leave the shipment on hold until capture is confirmed. The pre-image handles
`captured`, `pending` and `declined` separately. The patch broadens the first
condition so a pending result is marked paid and its shipment is released.
Captured and declined results are unchanged. A payment that later declines can
therefore leave an already released shipment without payment.

Ground truth is one `correctness` / `critical` finding on the added condition at
line 14, yielding `blocking`. The severity reflects the direct, systemic release
of every pending payment through this decision path, with immediate financial
exposure. It does not depend on an attacker controlling the gateway status. The
source is synthetic TypeScript; neither pre-image nor patch contains answer-hint
comments.

Run this dependency-free oracle from the repository root with Node 22. It checks
the public decision for all three gateway statuses before and after applying the
patch to a temporary copy of the pre-image:

```sh
node --input-type=module <<'JS'
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const fixture = resolve("test-prs-dataset/cases/LOG-02");
const temporary = mkdtempSync(join(tmpdir(), "log-02-oracle-"));

async function observe(file) {
  const javascript = stripTypeScriptTypes(readFileSync(file, "utf8"));
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`;
  const { decideFulfillment } = await import(moduleUrl);
  return ["captured", "pending", "declined"].map((status) =>
    decideFulfillment({ orderId: "A-204", status }),
  );
}

try {
  cpSync(join(fixture, "base"), temporary, { recursive: true });
  const file = join(temporary, "src/checkout/settlement.ts");
  assert.deepEqual(await observe(file), [
    { orderId: "A-204", paymentState: "paid", releaseShipment: true },
    { orderId: "A-204", paymentState: "awaiting_payment", releaseShipment: false },
    { orderId: "A-204", paymentState: "payment_failed", releaseShipment: false },
  ]);
  execFileSync("git", ["apply", join(fixture, "diff.patch")], { cwd: temporary });
  assert.deepEqual(await observe(file), [
    { orderId: "A-204", paymentState: "paid", releaseShipment: true },
    { orderId: "A-204", paymentState: "paid", releaseShipment: true },
    { orderId: "A-204", paymentState: "payment_failed", releaseShipment: false },
  ]);
  console.log("pending changes from held to shipment release");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
JS
```

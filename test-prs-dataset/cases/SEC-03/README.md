# SEC-03 oracle

The added `readReport` function joins a caller-controlled filename to the reports directory without checking the resolved path. After applying the patch, `readReport("monthly.txt")` returns `Public monthly report.`, while `readReport("../private/payroll.txt")` returns `Private payroll records.`. The second call crosses the intended reports boundary and exposes unrelated application data.

After applying the patch to a copy of `base/`, the behavior can be checked from that copy with Node 22:

```sh
node --input-type=module -e 'import { readReport } from "./src/report-reader.ts"; console.log(await readReport("../private/payroll.txt"))'
```

Ground truth is one `security` / `high` finding at added line 7, yielding `blocking`. No real private data or third-party source is used, and the patch has no answer-hint comments.

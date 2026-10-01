export type CheckSummaryInput = {
  passed: number
  failed: number
  skipped: number
}

export function formatCheckSummary({
  passed,
  failed,
  skipped,
}: CheckSummaryInput): string {
  const total = passed + failed + skipped
  return `${passed}/${total} checks passed (${skipped} skipped)`
}

export const STANDARD_BAGS_ML = [50, 100, 250, 500, 1000];

export function nearestBag(volumeMl: number): number {
  return STANDARD_BAGS_ML.reduce((best, size) =>
    Math.abs(size - volumeMl) < Math.abs(best - volumeMl) ? size : best,
  );
}

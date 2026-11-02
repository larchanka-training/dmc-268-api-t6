/**
 * Return one page of items. Pages are numbered from 1: page 1 holds the first `size` items.
 */
export function paginate<T>(items: T[], page: number, size: number): T[] {
  return items.slice((page - 1) * size, page * size);
}

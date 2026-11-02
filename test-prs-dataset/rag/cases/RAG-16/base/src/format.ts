export function shortTitle(title: string, max = 40): string {
  return title.length > max ? `${title.slice(0, max - 3)}...` : title;
}

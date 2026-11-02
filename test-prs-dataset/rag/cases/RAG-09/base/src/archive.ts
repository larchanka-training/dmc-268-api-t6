import { isoDate } from "./dates";
import type { Post } from "./post";

export function archiveKey(post: Post): string {
  return isoDate(post.publishedAt).slice(0, 7);
}

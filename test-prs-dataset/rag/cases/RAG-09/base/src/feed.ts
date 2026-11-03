import { slugify } from "./slug";
import type { Post } from "./post";

export function feedEntryId(post: Post): string {
  return `tag:inkwell.example,${slugify(post.title)}`;
}

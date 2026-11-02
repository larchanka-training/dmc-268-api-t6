import { isoDate } from "./dates";
import type { Post } from "./post";

export function slugify(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export function postPath(post: Post): string {
  return `/${isoDate(post.publishedAt)}/${slugify(post.title)}/`;
}

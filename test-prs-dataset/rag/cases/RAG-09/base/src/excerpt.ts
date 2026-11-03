import type { Post } from "./post";

export function excerpt(post: Post, limit = 120): string {
  return post.body.length > limit ? `${post.body.slice(0, limit)}...` : post.body;
}

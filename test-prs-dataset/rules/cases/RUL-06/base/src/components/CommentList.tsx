import type { Comment } from "../api/types";

export function CommentList({ comments }: { comments: Comment[] }) {
  return (
    <ul className="comments">
      {comments.map((comment) => (
        <li key={comment.id}>
          <strong>{comment.author}</strong>
          <p>{comment.body}</p>
        </li>
      ))}
    </ul>
  );
}

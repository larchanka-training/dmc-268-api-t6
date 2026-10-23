import DOMPurify from "dompurify";
import { marked } from "marked";

import { ALLOWED_ATTR, ALLOWED_TAGS } from "./sanitize";

export function renderTrustedMarkdown(source: string): string {
  const html = marked.parse(source, { async: false });
  return DOMPurify.sanitize(html, { ALLOWED_TAGS, ALLOWED_ATTR });
}

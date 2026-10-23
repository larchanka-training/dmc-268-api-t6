// GitHub Git Trees/raw Blob contracts, limited to the index policy reader.
// Root AGENTS.md is deliberately visible; reading its blob is unsupported (501).
const http = require("http");
const commit = "c".repeat(40);
const directory = "d".repeat(40);
const blob = "e".repeat(40);
const rules = "```review-ignore\nprivate/**\n```\n";
const cases = new Set([
  "hidden", "missing-root", "missing-rules", "blob-404", "blob-500",
  "truncated", "root-404",
]);
const entry = (path, type, mode, sha, size) => ({ path, type, mode, sha, size });
const agents = entry("AGENTS.md", "blob", "100644", "a".repeat(40), 13);
const review = entry(".review", "tree", "040000", directory);
const rule = entry("rules.md", "blob", "100644", blob, Buffer.byteLength(rules));

http.createServer((req, res) => {
  const answer = (status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  const match = /^\/repos\/fixture\/([^/]+)\/git\/(trees|blobs)\/([0-9a-f]{40})$/.exec(req.url);
  if (req.method !== "GET" || !match || !cases.has(match[1])) {
    return answer(501, { message: "Fixture does not model this request" });
  }
  if (req.headers.authorization !== "Bearer index-policy-fake-token" ||
      req.headers["x-github-api-version"] !== "2022-11-28") {
    return answer(400, { message: "Unexpected fixture request headers" });
  }
  const [, scenario, kind, oid] = match;
  if (kind === "trees" && oid === commit) {
    if (scenario === "root-404") return answer(404, { message: "Not Found" });
    return answer(200, {
      sha: commit,
      tree: scenario === "missing-root" ? [agents] : [agents, review],
      truncated: scenario === "truncated",
    });
  }
  if (kind === "trees" && oid === directory && scenario !== "missing-root") {
    return answer(200, { sha: directory, tree: scenario === "missing-rules" ? [] : [rule], truncated: false });
  }
  if (kind === "blobs" && oid === blob) {
    if (scenario === "blob-404") return answer(404, { message: "Not Found" });
    if (scenario === "blob-500") return answer(500, { message: "Internal Server Error" });
    if (req.headers.accept !== "application/vnd.github.raw+json") {
      return answer(400, { message: "Expected raw Git blob media type" });
    }
    res.writeHead(200, { "Content-Type": "application/vnd.github.raw+json" });
    return res.end(rules);
  }
  return answer(501, { message: "Fixture does not model this request" });
}).listen(process.env.PORT || 4907, "127.0.0.1");

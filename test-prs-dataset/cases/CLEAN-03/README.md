# CLEAN-03 source and oracle

This case is the complete three-file source diff from
[Zustand PR #663](https://github.com/pmndrs/zustand/pull/663) by dai-shi. The
GitHub PR API reports base `e3566f9b54c520343b9318720ac3dd9b8397bed7`
and head `2593c915195c0a75f8afbf02a5b90945721d27e1`. The pre-image files
are byte-identical to the base Git blobs: `src/index.ts`
`9f17bee494c83d942ad16b8df9261e00538f944b` and `src/vanilla.ts`
`b1e38ff2bcf0dcbf3927263006803db2a077c085`. Applying the original PR
patch yields head blobs `b4d7737d2bb237dabbff2d078a12821e119b6c69` for
`src/index.ts`, `bf7b6ba6e941dd372b6a59c889dbc2d6596d527d` for new
`src/react.ts`, and `027a66f55301a412bc460c2f76e1feba423bade9` for
`src/vanilla.ts`. The committed [MIT license at the base revision](https://github.com/pmndrs/zustand/blob/e3566f9b54c520343b9318720ac3dd9b8397bed7/LICENSE)
is byte-identical to blob `a2c2649deec2aabf248294a60a6e2e63a58f4a2b`
and retains the copyright notice.

The PR moves the React hook implementation from `src/index.ts` into
`src/react.ts`, retaining the public index re-exports. The moved source is
identical after the local import name changes from `createImpl` to
`createStore`; `src/vanilla.ts` only renames that local function and keeps its
default export. This is a clean source refactor with no expected findings.
The original source contains no benchmark answer-hint comments.

Run the dependency-free source-level oracle from the repository root:

```sh
node test-prs-dataset/cases/CLEAN-03/oracle.mjs
```

It applies the full PR patch and checks the base/head Git blob IDs, exact
React hook body after alias normalization, public re-export lines, vanilla
function rename, and unchanged license. The API repository does not ship a
React/TypeScript runtime, so this checks source equivalence and provenance
rather than executing a browser hook.

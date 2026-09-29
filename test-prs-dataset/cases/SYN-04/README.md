# SYN-04 oracle and provenance

This case is the complete `src/middleware/devtools.ts` file diff from
[Zustand PR #725](https://github.com/pmndrs/zustand/pull/725). GitHub's PR API
reports base `76eeacb1c448ea323f46c2956ffb66c307c746b5`, head
`9d6e7e2050a5b9b4b541f0df7708f30d74cbe760`, and author devanshj. The
committed pre-image is byte-identical to the [base file](https://github.com/pmndrs/zustand/blob/76eeacb1c448ea323f46c2956ffb66c307c746b5/src/middleware/devtools.ts)
(Git blob `72caf95957d90e2be19290e2c9cae5d1fa33de28`). Applying `diff.patch`
produces the byte-identical [head file](https://github.com/pmndrs/zustand/blob/9d6e7e2050a5b9b4b541f0df7708f30d74cbe760/src/middleware/devtools.ts)
(Git blob `f19f93f34d3b1589d14c3f94db750fa83497eea1`). Its 189 additions and
200 deletions match the PR changed-files API for this source file. Other PR
files are outside this case.

The source has the [MIT license at the base revision](https://github.com/pmndrs/zustand/blob/76eeacb1c448ea323f46c2956ffb66c307c746b5/LICENSE). The full notice is retained in
`base/.upstream/LICENSE`; attribution is recorded in `case.json`. The validator
applies the original upstream patch with `--directory=.upstream`.

The base file already declares `StoreMutators<S, A>` for `'zustand/devtools'`.
The PR adds an identical `declare module '../vanilla'` block at new-side line
86. This repeats a type declaration without adding a member or changing
behavior. [Fix PR #3443](https://github.com/pmndrs/zustand/pull/3443)
explicitly removes the second block and confirms that TypeScript merges the
duplicate declarations. The expected truth is `readability` / `low` at added
line 86, yielding `attention`. The other changes in PR #725 are outside this
case's ground truth. Neither the pre-image nor patch contains a benchmark
answer hint.

Run the dependency-free oracle from the repository root:

```sh
node test-prs-dataset/cases/SYN-04/oracle.mjs
```

It applies the exact source patch in a temporary copy and checks that one
augmentation block becomes two byte-identical blocks with the same member.
The repository has no TypeScript compiler dependency, so this local oracle
checks the duplication directly; the type-merging semantics are corroborated
by the upstream fix PR.

# Rules guide

Ready `.review/rules.md` templates for a repository reviewed by AI Code Reviewer. This guide helps
you pick one and fit it to your project. The file format, the mask syntax and the limits are in
[`docs/RULES_FORMAT_SPEC.md`](../../docs/RULES_FORMAT_SPEC.md), and this guide does not repeat them.

## Pick and copy

| Template | For |
| --- | --- |
| `python-fastapi` | Python services on FastAPI |
| `typescript-react` | TypeScript frontends on React |
| `go` | Go services |
| `security-baseline` | Any stack, on its own or with a stack template |

Copy the folder to the root of your repository, then merge it:

```bash
cp -r examples/rules/go/.review /path/to/your/repository/
```

The review reads the file from the commit a pull request is based on
([§3.1](../../docs/RULES_FORMAT_SPEC.md#31-ревизия-и-тип-объекта)): the template applies only after
the merge, and the pull request that adds it is not reviewed by its rules.

## Fit it to your project

The service sends the whole file to the model and does not sort rules by language or path, so
every line costs tokens and shapes the review.

- The first line names what to change: the paths and the masks. Change them, then delete that line.
- Delete each rule your project does not follow. A rule about a helper or a folder you do not have
  can produce false findings.
- Write your own rules the way the templates do: what is required, where, why, and
  `Report … as a security|correctness|performance|readability problem`. In a small
  [A/B run](../../docs/test-plan/RULES_MD_TEST_REPORT.md) on review prompt v5 (5 constraint cases,
  one model), the rules of the eval corpus raised the share of constraints found from 40 % to 80 %.
  That measures those rules, not these templates, and promises no gain for your project.
- Leave out what a linter or a type checker enforces: the service
  [drops such findings](../../review/postprocess/lint-filter.md) anyway.

## Combine two templates

A repository has one `.review/rules.md`, and that file has at most one `review-ignore` block. Two
templates pasted together as they are fail every run with `policy_ignore_block_duplicate`. To add
`security-baseline` to a stack template:

1. Paste its sections above the `# Ignore` section of the stack template.
2. Move its masks into the block of the stack template and delete its own block.
3. Delete its first line too.

## Size

| Template | Bytes | Tokens |
| --- | --- | --- |
| `go` | 2112 | 1130 |
| `python-fastapi` | 2467 | 1302 |
| `security-baseline` | 2061 | 1080 |
| `typescript-react` | 2113 | 1137 |
| Largest stack + `security-baseline` | 4528 | 2382 |

A file may take up to 64 KiB. Tokens are the service's estimate of the block the model sees, at 3
characters per token. That block marks up every line, so the number is about 60 % above an estimate
from the size of the file. The last row is a plain sum, an upper bound of the merged file.

`.review/rules.md` and the root `AGENTS.md` share a budget of 12,000 tokens, and a run over it fails
with `policy_prompt_budget_exceeded`. Each template stays under 6,000, which leaves at least half of
the budget to your `AGENTS.md`. Keep to that when you add rules.

## Exceptions

Nothing in this file guarantees what a review reports. The two kinds of exception work differently:

- **A text exception** ("do not report X in Y") is a request to the model, and it may not hold. On
  review prompt v5 the reviewer respected 28 % of them
  ([#399](https://github.com/larchanka-training/dmc-268-api-t6/issues/399)). The templates have none.
- **A mask in `review-ignore`** keeps the content of matching files away from the model, and the
  service applies it without asking the model. It does not exempt the code that uses those files,
  and it removes the whole file from the review, real defects included. The templates mask only
  generated code, vendored code and build output.

The service already leaves out lock files, minified JavaScript, protobuf Go files and a root `dist/`
([`is_generated_path`](../../app/modules/reviews/application/vcs_diff.py)), so the templates do not
list them.

Review a change to `.review/rules.md` the way you review code. Its text steers the model, and a
hostile file can make the reviewer hide real findings
([§2.1](../../docs/RULES_FORMAT_SPEC.md#21-модель-угроз-и-пределы-защиты),
[#362](https://github.com/larchanka-training/dmc-268-api-t6/issues/362)).

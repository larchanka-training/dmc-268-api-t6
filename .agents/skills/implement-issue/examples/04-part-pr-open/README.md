<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/04-part-pr-open/README.md -->

# 04 — Part 1 has an open pull request: stop and name it

**Source:** synthetic, from the fixture of `03-next-part`: ui#213 is OPEN instead of MERGED and
api#382 (the SYNC copy) is not filed yet.

## Input

```sh
preflight.py api#266
```

## Expected

- `verdict: stop`, exit code 1, `selected: null`.
- One reason: Part 1 already has an open pull request, ui#213, "finish it or pass --part".
- Ч.1 is `open_pr` with ui#213 (OPEN); Ч.2 is `todo`.
- The skill reports the reason verbatim and ends: starting Part 2 over an unfinished Part 1 is
  the human's call, made with `--part 2` (case 05).

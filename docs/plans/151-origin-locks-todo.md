# Origin-lock refactor todo (#151 P3-5)

- [x] Read approved combined plan, detailed brief, AGENTS and backend/git rules.
- [x] Verify independent main base and open-PR ownership.
- [x] F1: record zero-skip baseline, extract typed lock helper and migrate eight sites.
  Before source: 140 passed, zero skipped, 180.94s. Restored-source static gates pass.
  Run-first mutation fails all five NOWAIT authority-lock regressions as expected.
- [x] F2: record zero-skip eight-file acceptance and four required gates.
  Acceptance: 256 passed, zero skipped, 372.57s.
  Full gate: 3425 passed, zero skipped, 753.16s; all three static gates pass.
- [x] Fresh independent Standards/Spec review (coordinator): zero findings in both axes
  against exact base c1d8f59 and the stable four-file working diff.
- [x] External first-PR public composition test passes against this app with import provenance checked.
- [x] Publish independent PR (publisher): [#153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153), base main.
  Source/proof commit `84641aa`; no reviewer assignments. CI/Ready is tracked by
  the coordinator; human approval, merge and issue acceptance remain external.

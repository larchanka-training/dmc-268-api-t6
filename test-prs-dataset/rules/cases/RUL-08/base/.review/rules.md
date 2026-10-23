# TypeScript

## No any in hand-written code

`any` is not allowed in hand-written code. Exception: files under `src/generated/`
are produced by `openapi-typescript` from `contracts/` and may contain `any`; do
not report `any` there, the fix belongs in the contract.

## Browser storage

Feature code reads and writes persisted settings only through `readSetting` and
`writeSetting` from `src/lib/storage.ts`, which namespace every key per workspace.
Direct `localStorage` access under `src/features/` is a defect.

# Ignore

```review-ignore
dist/**
coverage/**
```

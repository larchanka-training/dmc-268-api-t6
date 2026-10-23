# Security

Never commit real credentials. Service secrets come from environment variables
read in `app/settings.py`.

## Test fixtures

Files under `tests/fixtures/` hold throwaway values for the local GitHub stub
(`scripts/github_stub.py`), which accepts nothing else. A literal such as
`FAKE_TEST_CREDENTIAL_DO_NOT_USE` there is intended: do not report it as a
hard-coded secret.

## Webhooks

Webhook signatures are verified before the body is parsed.

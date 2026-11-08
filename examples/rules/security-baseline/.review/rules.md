Template for any stack, a security baseline; adapt to your project: the source of secrets, the names of your webhooks, the masks at the end.

# Security

## Secrets

Never commit real credentials, fixtures and examples included. Service secrets come
from environment variables or a secret manager. Tokens, passwords, API keys and session
ids stay out of logs, error messages and exception reports. Report a violation as a
security problem.

## Injection

SQL that includes user input must be parameterized. A shell command is built as an
argument list, without a shell. A file path built from user input is resolved and
checked to stay inside its base directory. Report a violation as a security problem.

## Access to objects

An id in a URL or body is user input. A route that reads or changes an object checks
that this caller may access this object, not only that the caller is logged in. Report
a lookup by such an id without an owner or tenant check as a security problem.

## Webhooks

A webhook signature is verified over the raw bytes, before the body is parsed, with a
constant-time comparison. Report anything else as a security problem.

## Cryptography

Passwords are hashed with Argon2, scrypt or bcrypt. Tokens come from a cryptographic
random source. MD5 and SHA-1 protect nothing. TLS verification stays on, so `verify=False`,
`InsecureSkipVerify` and `rejectUnauthorized: false` are violations. Report a violation as
a security problem.

## Untrusted data

Data from outside the repository does not reach `pickle`, `eval`, `exec`, a non-safe
YAML loader or a template compiler. A URL taken from user input is requested only after
its host passes an allow-list, redirects included. Report a violation as a security
problem.

## Browser-facing answers

CORS does not combine credentials with `*` or a reflected origin. A session cookie has
`HttpOnly`, `Secure` and `SameSite`. Report a violation as a security problem.

# Ignore

Coverage reports and source maps are build output, not a review source.

```review-ignore
coverage/**
**/*.map
```

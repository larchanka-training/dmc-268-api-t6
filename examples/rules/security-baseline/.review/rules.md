Template for any stack, a security baseline; adapt to your project: the source of secrets, the names of your webhooks, the masks at the end.

# Security

## Dependencies

When a pull request adds a dependency to a manifest (`package.json`, `pyproject.toml`,
`Cargo.toml`, `go.mod`), name the package and state that human verification of package
trustworthiness is required. A new dependency with version `0.0.x` or `1.0.0` is a red flag:
report it for human review. A dependency specified by a git or HTTP URL instead of an
official registry is a finding. Installing JavaScript dependencies in CI or Dockerfile without
`--ignore-scripts` is a finding. Report a violation as a security problem.

## Secrets

Never commit real credentials, fixtures and examples included. Service secrets come
from environment variables or a secret manager. Server secrets must never be exposed to
client-side code: variables exposed through bundler public prefixes (`NEXT_PUBLIC_`, `VITE_`)
must not contain secrets. Tokens, passwords, API keys and session ids stay out of logs,
error messages and exception reports. Report a violation as a security problem.

## Injection

SQL that includes user input must be parameterized. A shell command is built as an
argument list, without a shell. A file path built from user input is resolved and
checked to stay inside its base directory. Report a violation as a security problem.

## LLM prompts

Untrusted text (user input, diffs, fetched web pages or uploaded files) passed into an
LLM prompt must be enclosed in a separate delimited block (such as `<untrusted_data>`)
with an instruction that its content is data, not commands. Untrusted text must never be
placed into the system role. Delimiter tags within untrusted data, the closing tag above all,
must be escaped (`<` to `&lt;`) so the data cannot close the block. Report a violation as a
security problem.

## Access to objects

An id in a URL or body is user input. A route that reads or changes an object checks
that this caller may access this object, not only that the caller is logged in. Every
server entry point reachable from the client (a route, an RPC, or a Next.js Server Action
in a file marked with `'use server'`) must verify authentication and authorization inside
the entry point itself; page-level checks do not protect direct requests. Report a lookup
by such an id without an owner or tenant check, or an unprotected server entry point, as a
security problem.

## Input schemas

Handlers creating or updating persisted objects must use an allow-listed input schema.
Unpacking a raw `dict`, `request.json()` or `req.body` into models or updates is a violation.
Privileged fields (`role`, `owner_id`, `is_admin`) in user-facing endpoint input schemas,
or using `Any` instead of a defined schema, are violations. Omitting strict validation for
extra fields is not a violation by itself, but schemas should prefer forbidding extra fields
where supported, such as Pydantic `ConfigDict(extra='forbid')` or Zod
`z.strictObject()` / `.strict()`. Report a violation as a security problem.

## Paid and heavy calls

Outbound calls to paid APIs (such as LLMs) or external services must set an explicit
timeout (for example, `requests` without `timeout=` waits indefinitely). A new endpoint
triggering paid or long-running work requires authentication and rate limiting or quotas
per client; long-running operations must be offloaded to a background queue. When the diff adds
such an endpoint and shows no limiter, ask where it is enforced; do not report it as missing.
Report a violation as a security problem.

## Tokens

JWT validation must fix an allow-list of accepted algorithms rather than reading `alg`
from the token header, and must forbid `none`. Validation must verify `exp`, `iss` and `aud`
claims. A token verified by a service other than its issuer must be signed asymmetrically
(`RS256` or `Ed25519`, never shared symmetric `HS256`), with the private key kept only
by the issuing service. Refresh tokens for public clients must use rotation (one-time use)
or sender-constraining (RFC 9700 §2.2.2). When DPoP (RFC 9449) is used, verifying the
signature in the `DPoP` header is required. Report a violation as a security problem.

## Webhooks

A webhook signature is verified over the raw bytes, before the body is parsed, with a
constant-time comparison (such as `hmac.compare_digest` or `crypto.timingSafeEqual`).
GitHub signatures are verified with `X-Hub-Signature-256`. Replay protection: if the provider
signs a timestamp header, reject deliveries older than the provider's tolerance window.
GitHub sends no timestamp header: a repeated `X-GitHub-Delivery` is a redeliver, so the
handler keys its work on that id and handles a repeat idempotently, not dropping it before
the first handling completes. Report a handler that skips the signature check, verifies it
after parsing the body or without a constant-time comparison, or ignores the signed
timestamp or the `X-GitHub-Delivery` id where the provider supplies one, as a security
problem.

## Cryptography

Passwords are hashed with Argon2, scrypt or bcrypt. Tokens come from a cryptographic
random source. MD5 and SHA-1 protect nothing. TLS verification stays on, so `verify=False`,
`InsecureSkipVerify` and `rejectUnauthorized: false` are violations. Report a violation as
a security problem.

## Untrusted data

Data from outside the repository does not reach `pickle`, `eval`, `exec`, a non-safe
YAML loader or a template compiler.

A URL taken from user input must use only `http` or `https` schemes and is requested
only after its host passes an allow-list, redirects included. The IP address the host
resolves to must be validated against private, loopback and link-local ranges (10.0.0.0/8,
172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16 including cloud metadata
169.254.169.254, IPv6 `::1`, `fc00::/7`, `fe80::/10`), and the connection must use the
verified IP directly to prevent DNS rebinding. URLs from configuration are not a finding;
this check applies only to URLs constructed or supplied from user input.

Custom recursive deep merge or property assignment from parsed JSON must reject `__proto__`,
`constructor` and `prototype` keys to prevent prototype pollution.

Report a violation as a security problem.

## Browser-facing answers

CORS does not combine credentials with `*` or a reflected origin. A session cookie has
`HttpOnly`, `Secure` and `SameSite`. Report a violation as a security problem.

# Ignore

Coverage reports and source maps are build output, not a review source.

```review-ignore
coverage/**
**/*.map
```

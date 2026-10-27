# Bitbucket Cloud repository webhook (#333)

The browser connection API is in [`contracts/openapi.yaml`](../contracts/openapi.yaml).
This receiver is a separate provider-to-API contract and is deliberately absent
from that browser OpenAPI document.

## Setup

After connecting a repository, the portal shows a one-time URL and secret for
that repository. Configure a **repository webhook** in Bitbucket Cloud with
that URL and secret and subscribe to `repo:push`, `pullrequest:created` and
`pullrequest:updated`. The public URL is
`https://<api-host>/webhooks/bitbucket/{installation_id}/{repository_id}`;
`BITBUCKET_WEBHOOK_BASE_URL` supplies the HTTPS origin. The reserved
`staging-webhook` host returns 503; staging uses the public `staging-api` host.

Each repository binding has a separate secret. The portal returns it only on
initial connection or explicit secret rotation. Rotation invalidates the old
signature; disconnect revokes that repository binding and its webhook while
leaving sibling repository bindings active. Remove the obsolete webhook from
Bitbucket's settings as well.

## Delivery contract

Send `POST` with the original JSON body bytes, `X-Hub-Signature` in
`sha256=<hex>` form, `X-Event-Key`, `X-Hook-UUID` and `X-Request-UUID`.
`X-Attempt-Number` is optional and must be a decimal integer from 1 to 1000.
The receiver caps the body at 256 KiB, rejects duplicate/malformed headers and
verifies HMAC over raw bytes before parsing JSON. The payload repository UUID
must match the active binding. The first valid signed receipt activates only
that repository's webhook status; an invalid signature never activates it.

| HTTP | Meaning |
| --- | --- |
| 202 | Receipt committed or exact duplicate accepted; `status` reports its durable state. |
| 400 | Malformed header, JSON or payload. |
| 401 | Missing/invalid signature or inactive origin. |
| 409 | Reused delivery identity with different payload. |
| 413 | Body exceeds 256 KiB. |
| 503 | Receipt store unavailable. |

The durable receipt namespace includes the repository binding. A retry may
repeat the delivery identity; duplicate effects are also prevented by the
canonical `(pull request, head)` Run key. The backend refetches current PR
state before creating a Run, so late events cannot restore an older head.
The provider's retry UUID stability still requires the disposable live probe.

See [Atlassian webhook signatures](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/)
and [event headers and payloads](https://support.atlassian.com/bitbucket-cloud/docs/event-payloads/).

# Meta webhook signature verification — v1

Status: implemented on the development PR branch; not deployed.

## Runtime behavior

- Every POST handled by the legacy Flask webhook verifies the `X-Hub-Signature-256` header against the original request body using HMAC-SHA256.
- The secret is read from the `META_APP_SECRET` environment variable.
- Missing configuration fails closed with HTTP 503.
- Missing, malformed, or incorrect signatures are rejected with HTTP 403.
- Signature verification runs before JSON parsing, Redis checks, deduplication, media handling, or message processing.
- `app_customer.py` uses the same Flask app and route from `app.py`; its startup readiness log also reports whether `META_APP_SECRET` is configured.

## Required rollout steps

1. Set `META_APP_SECRET` in the Render staging service using the secret from the corresponding Meta app.
2. Set the matching secret in production only when the production release is approved. Do not copy a secret from a different Meta app.
3. Deploy to staging and verify the startup readiness log reports `meta_app_secret=ok`.
4. Test a genuine Meta webhook delivery and confirm it is processed.
5. Confirm a request without a signature and a request with an invalid signature are rejected.
6. Only after staging passes, review and approve the PR and plan the production release.

Do not put the secret in source control, issue comments, logs, or chat messages.

## Automated tests

The Python CI workflow compiles `app.py` and `app_customer.py`, then runs the webhook signature tests for valid signature, missing signature, invalid signature, tampered body, and missing secret.

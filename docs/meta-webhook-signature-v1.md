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


## Audit integrato cross-repository — 27 ottobre 2026

- PR bot #2 resta Draft e non unita; head verificato `ee9127f480746f83cb301822be153ddbfb993244`. La run #40237092071 è **success**; log letto: **62 test, OK**, inclusi test con servizio Redis effimero per lease/recovery. Non prova un restart reale del processo/servizio Render né lo storage audio reale.
- La PR API #3 (repository `VocalFlash/vocalflash`) resta Draft; i check sul head noto `737d42184f618ff6abc009df7af241f30d29131a` sono verdi. La PR API #10 aggiunge la conferma atomica, ma il suo Preview non è pronto per E2E: le variabili `VF_ASSISTANT_*` e `VOCALFLASH_ASSISTANT_API_KEYS` risultano associate al branch `assistant-auth-v1`, mentre `OWNER_COMMAND_CONFIRMATION_TTL_SECONDS` manca. Nessun segreto è stato decifrato o modificato.
- La prova di CI non chiude gli issue bot #3 (outbox/reconciliation), #4 (invii WhatsApp ambigui) e #5 (namespace/lease/recovery): rimangono aperti outbox durevole, esito `outcome_unknown`, callback/riconciliazione, restart effettivo Render/Redis, durabilità Redis, inventario e drain del namespace legacy, storage audio reale e ownership degli oggetti.
- La firma webhook Meta è implementata nel branch candidato ma non è provata attiva su Render staging/produzione; `META_APP_SECRET` va configurato nel target autorizzato e verificato con richieste firmate/non firmate prima del deploy.
- **Decisione:** nessun merge, deploy Render, cambio variabili live, invio WhatsApp reale o mutazione DB. La consegna rimane at-most-once, non exactly-once; nessun retry cieco dopo esito ambiguo.

Per completare l'audit runtime Render serve selezionare esplicitamente il workspace nell'integrazione Render. L'inventario del 25 ottobre resta l'ultima osservazione disponibile: staging su `34c63efb6e0e720e8dc07c10c3c457a5d59ae5b5`, produzione su `788eecf280e0bb6eb22424669b89ed7ad0abc432`, Redis Free/Oregon con persistence off. Non assumere che tale stato sia ancora corrente senza una nuova lettura.

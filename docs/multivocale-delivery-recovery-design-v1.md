# MultiVocale Outbound Delivery Recovery Design v1

**Status:** design/audit only. No runtime, database schema, Supabase, Meta configuration, or production behavior changed by this document.  
**Reviewed:** 2026-10-23  
**Scope:** MultiVocale reply delivery in `VocalFlash/vocalflash-bot`, coordinated with the Assistant API release.  
**Release status:** NO-GO for claiming durable, duplicate-safe outbound delivery.

## 1. Current implementation facts

Reviewed on bot branch `assistant-conversation-photo-v1` at commit `2dab3626203f85c707e81eaadc965fbf4bb98b45`:

- `PersistentMultivocaleRuntime._process_batch` persists `ready_answer`, renews the processing lease, calls `legacy.safe_send`, then calls `finish_batch(completed)`.
- If the send is accepted by Meta but the process or Redis write fails before `finish_batch`, the batch can remain recoverable with `ready_answer`; a later worker can send the same reply again.
- The current exception path attempts `mark_failed` and sends a retry prompt. If the original send had an ambiguous outcome, this prompt can itself be misleading and a user-triggered retry can duplicate the original answer.
- `app.py::send_whatsapp_message` logs the HTTP status and calls `raise_for_status()`, but discards the response body and Meta message ID. The outbound payload does not set `biz_opaque_callback_data`.
- The signed Meta webhook currently processes `value.messages`; it does not reconcile `value.statuses` delivery notifications.
- The Redis processing lease and fencing token protect state mutations that validate the token. They cannot make an external HTTP side effect atomic with a Redis state update.
- The existing inbound deduplication TTL is not an outbound delivery ledger and does not provide durable exactly-once delivery.
- Audio/batch state and cleanup still have Redis/object-storage lifecycle constraints; this design must not claim to solve those unrelated persistence gaps.

These facts establish the failure boundary; they do not prove that a duplicate has occurred in production.

## 2. Required invariants

1. Every logical outbound reply has one stable application-generated `delivery_id` and one durable record before any provider send attempt.
2. The logical idempotency key is unique and derived from trusted identifiers, e.g. business/channel + source batch/event + reply purpose/version. Do not include customer text or phone numbers in logs or keys where a stable opaque ID is available.
3. A worker must atomically claim a pending delivery before attempting to send it. Concurrent workers cannot both own the same attempt.
4. A local lease/fencing token protects local writes only; it must never be described as a provider-side idempotency guarantee.
5. A positive provider response stores the provider message ID and response metadata before the record is considered accepted.
6. A timeout, connection reset, process crash, or unreadable provider response after request transmission is `outcome_unknown`, not a definitive failure.
7. An `outcome_unknown` record is not automatically resent unless a documented provider guarantee or reliable reconciliation proves that resend is safe.
8. Status callbacks are authenticated, correlated, idempotently applied, and cannot regress a terminal/advanced state because they arrived out of order.
9. The business reply and the “retry” instruction must not contradict one another. Do not tell the user to retry the original work when the prior reply may already have been sent.
10. Secrets, tokens, recipient IDs, message bodies, audio, media URLs, and raw provider payloads must not be emitted into routine logs or test output.

## 3. Proposed durable state machine

Minimum states:

- `pending`: intent durably created; no provider attempt started.
- `sending`: an attempt was durably recorded and claimed by a worker.
- `accepted`: Meta returned a valid successful response and a message ID was persisted.
- `sent`: optional alias only if the application needs to represent provider acceptance separately from delivery; do not use as a synonym for delivered.
- `delivered`: an authenticated provider status callback confirms delivery.
- `read`: optional provider status, if enabled and relevant; never required to finish processing.
- `failed_definitive`: a response documented to mean this attempt was rejected and no message was accepted.
- `outcome_unknown`: provider acceptance cannot be determined safely.
- `cancelled`: cancelled before any external send attempt.

Prefer the canonical sequence `pending -> sending -> accepted -> delivered/read`. `sending -> failed_definitive` is allowed only for documented definitive responses. `sending -> outcome_unknown` is required for ambiguous network/process failures. Do not reset `accepted` or `delivered` to `pending` as a retry shortcut.

Store attempt history separately or as append-only attempt records where feasible. A mutable current state alone is insufficient for incident analysis.

## 4. Suggested record contract (not a migration)

A future schema proposal should consider:

### Outbound intent
- opaque `delivery_id` (primary key)
- `idempotency_key` (unique)
- trusted business/channel identifiers
- source type and opaque source ID (batch/event)
- reply purpose/version
- recipient reference or encrypted/appropriately protected recipient field, according to the existing privacy model
- message payload or protected payload reference, with bounded retention
- state, attempt count, next eligible attempt time
- current lease owner/token and expiry
- provider message ID, accepted/delivered/read timestamps
- last sanitized error class/code
- created/updated timestamps and expiry/retention metadata

### Attempt record
- opaque attempt ID and delivery ID
- attempt number, worker claim token, started/finished timestamps
- outcome classification (`accepted`, `definitive_rejection`, `outcome_unknown`)
- HTTP status and allow-listed provider error code, where safe
- provider message ID when available
- no access token, raw body, customer content, or full phone number

### Callback receipt / reconciliation
- provider status event ID if supplied; otherwise a carefully designed dedupe key based on provider message ID, status, timestamp, and recipient reference
- provider message ID and delivery ID correlation
- received timestamp and normalized status
- signature verification outcome and minimal provenance
- idempotent unique constraint to prevent applying the same callback more than once

The actual schema, indexes, RLS/privileges, retention, and migration order require a separate review and explicit authorization. Do not create or modify tables as part of this audit.

## 5. Send protocol

1. Create the outbound intent durably using a unique idempotency key. Repeated creation returns the existing intent.
2. Atomically claim the intent and persist the attempt as `sending` before making the HTTP request.
3. Include the opaque `delivery_id` as `biz_opaque_callback_data` where supported by the exact Graph API version and message type. This value is correlation metadata, not an idempotency key and not proof of acceptance.
4. Send once for that attempt.
5. On a validated 2xx response with a valid provider message ID, persist `accepted` and the ID. If that persistence fails, treat the durable record as potentially `outcome_unknown`; do not blindly resend.
6. On a provider response that is documented as a definitive rejection, persist `failed_definitive` and the sanitized error class/code.
7. On timeout, connection reset, ambiguous 5xx, malformed/lost response, or process death after request transmission, leave/recover as `outcome_unknown` after lease expiry. Never assume no send occurred.
8. A stale worker may not overwrite a newer attempt's record. Every state update checks the current claim token/version.
9. The caller returns a result object that distinguishes provider acceptance from delivery confirmation. Do not collapse all outcomes into a boolean.

A provider 5xx must not automatically be considered definitive without validating the endpoint's documented semantics. Classify errors conservatively.

## 6. Reconciliation and callback handling

- Extend the already signature-verified webhook processing to inspect `value.statuses` as well as inbound `value.messages`.
- Verify the exact Meta callback shape, event ordering, supported `biz_opaque_callback_data` behavior, and whether callbacks can arrive before the local response is persisted.
- Correlate primarily by provider message ID; use the opaque callback data as a recovery correlation aid when available.
- If a callback arrives before the HTTP response write, allow it to attach to the existing delivery intent and ensure the later HTTP response cannot regress the state.
- Apply status callbacks idempotently. Unknown delivery IDs and unmatched provider IDs go to bounded sanitized diagnostics, not arbitrary record creation or outbound action.
- Reconciliation worker scans expired `sending` leases and old `outcome_unknown` records. It can query a provider only if a supported authoritative lookup exists; do not assume Meta offers a general lookup by application delivery ID.
- If there is no authoritative way to resolve an ambiguous outcome, keep the record `outcome_unknown` and surface it for controlled/manual resolution rather than automatically risking a duplicate.
- Define callback retention, retry behavior, and alert thresholds. Callback delivery alone cannot be treated as guaranteed to arrive.

## 7. MultiVocale runtime integration

- Keep synthesis idempotent separately from delivery. `ready_answer` is generated content, not proof that it was sent.
- Once the synthesis result is persisted, create or get the unique outbound intent for that batch/reply version.
- The batch may be marked `completed` only when the product's chosen completion contract is satisfied. Prefer separating `processing/summarized` from `delivery_state`; do not overload one batch status to mean both synthesis and external delivery.
- On `accepted`, the system may stop generating a new reply, but should keep delivery tracking active until a callback or retention policy settles it.
- On `outcome_unknown`, do not send the normal “Premi Riprova” instruction as though the first response definitely failed. Provide a distinct safe status or defer user-facing messaging until reconciliation.
- The extra “Vuoi creare un riepilogo unico di più vocali?” prompt is a separate outbound intent with its own idempotency key; otherwise the main reply may be deduplicated while the follow-up duplicates.
- Any retry button/action must reference the existing batch and delivery record and must not create a new logical reply without an explicit, auditable decision.

## 8. Required tests before implementation can be accepted

### Unit/state-machine
- Unique idempotency key returns the same intent under concurrent creation.
- Two workers cannot claim the same pending delivery.
- Stale claim tokens cannot update or finish a newer attempt.
- Every valid/invalid state transition is tested, including callback order permutations.
- Repeated callbacks are idempotent; callbacks for unknown IDs do not create outbound work.
- Payload, credentials, phone numbers, and raw callback bodies are absent from logs.

### Send failure boundaries
- Definitive 4xx rejection: no automatic resend unless the error contract specifically allows corrected retry.
- Meta accepts and the client receives the response: message ID persists, then callback moves state forward.
- Meta accepts but client times out: `outcome_unknown`; no blind retry.
- Connection fails before any request bytes are sent, if the transport can prove that fact: classify separately and test.
- Process dies before request, during request, after provider acceptance, after response received but before persistence, and after persistence.
- Persistence fails before send: no send occurs.
- Persistence fails after acceptance: no blind resend; reconciliation path remains available.
- Callback arrives before response persistence, after persistence, duplicated, delayed, and out of order.
- Redis/database outage during claim, send, state write, callback processing, and reconciliation.
- Restart with pending, sending-expired, accepted, delivered, and outcome-unknown records.

### Integration / staging
- Real signed Meta callback with sanitized evidence.
- A controlled test recipient and non-production credentials only.
- Exact bot and API candidate SHAs recorded.
- Redis/database concurrency tests and restart tests.
- No production side effects without explicit release authorization.

## 9. Release gates and non-goals

Release remains NO-GO for a claim of duplicate-safe durable delivery until:
- a durable outbox schema and migration are reviewed and explicitly authorized;
- send API result semantics and callback correlation are implemented;
- the webhook processes status callbacks safely;
- the failure-boundary and concurrency tests pass against the exact candidate;
- staging integration evidence is captured;
- retention, privacy, access control, monitoring, and rollback are reviewed.

This design does not promise mathematical exactly-once delivery. If the provider offers no idempotent send primitive and no authoritative way to resolve a lost response, some cases are fundamentally ambiguous; the correct policy is to preserve that ambiguity instead of blindly retrying.

No schema, secret, environment variable, paid resource, production deployment, or live data was changed while preparing this document.

## 10. Implemented conservative guard — 24 ottobre 2026

A first runtime guard has been implemented on the Draft branch after static review and focused unit/state tests:

- Persist `delivery_state="sending"` under the current lease before calling the WhatsApp send function.
- Persist `delivery_state="sent"` after a positive return from the send function and before post-send cleanup.
- If the send function reports failure, or any exception occurs after the send boundary, transition the batch to `delivery_unknown` / `outcome_unknown` where Redis is available.
- Do not expose the normal `vf_retry` action after crossing the send boundary.
- `claim_processing` refuses recovery of expired batches whose delivery state is `sending`, `sent`, or `outcome_unknown`; this prevents an expired lease from silently replaying the external side effect.
- Focused tests cover an expired lease after a send attempt, terminal unknown outcome, failed/ambiguous send, and a completion-persistence failure after the reply was sent.

Evidence: CI run [40081928311](https://github.com/VocalFlash/vocalflash-bot/actions/runs/40081928311) passed on candidate commit `1a717d314ab07ea670c4e20a0bb0dd8b676c469b`.

**Limitations remain:** `safe_send` still collapses provider outcomes to a boolean; the guard therefore treats any reported send failure conservatively as ambiguous, even when a rejection might be definitive. There is not yet a durable outbound intent/outbox, persisted Meta message ID, status-callback reconciliation, operator resolution interface, or true delivery-status lifecycle. The `sent` state is only the bot's observation of a positive HTTP-level send response, not proof that the recipient received/read the message. This is a risk-reduction step, not a claim of exactly-once delivery and not sufficient for release.


## Audit integrato cross-repository — 27 ottobre 2026

- PR bot #2 resta Draft e non unita; head verificato `ee9127f480746f83cb301822be153ddbfb993244`. La run #40237092071 è **success**; log letto: **62 test, OK**, inclusi test con servizio Redis effimero per lease/recovery. Non prova un restart reale del processo/servizio Render né lo storage audio reale.
- La PR API #3 (repository `VocalFlash/vocalflash`) resta Draft; i check sul head noto `737d42184f618ff6abc009df7af241f30d29131a` sono verdi. La PR API #10 aggiunge la conferma atomica, ma il suo Preview non è pronto per E2E: le variabili `VF_ASSISTANT_*` e `VOCALFLASH_ASSISTANT_API_KEYS` risultano associate al branch `assistant-auth-v1`, mentre `OWNER_COMMAND_CONFIRMATION_TTL_SECONDS` manca. Nessun segreto è stato decifrato o modificato.
- La prova di CI non chiude gli issue bot #3 (outbox/reconciliation), #4 (invii WhatsApp ambigui) e #5 (namespace/lease/recovery): rimangono aperti outbox durevole, esito `outcome_unknown`, callback/riconciliazione, restart effettivo Render/Redis, durabilità Redis, inventario e drain del namespace legacy, storage audio reale e ownership degli oggetti.
- La firma webhook Meta è implementata nel branch candidato ma non è provata attiva su Render staging/produzione; `META_APP_SECRET` va configurato nel target autorizzato e verificato con richieste firmate/non firmate prima del deploy.
- **Decisione:** nessun merge, deploy Render, cambio variabili live, invio WhatsApp reale o mutazione DB. La consegna rimane at-most-once, non exactly-once; nessun retry cieco dopo esito ambiguo.

Per completare l'audit runtime Render serve selezionare esplicitamente il workspace nell'integrazione Render. L'inventario del 25 ottobre resta l'ultima osservazione disponibile: staging su `34c63efb6e0e720e8dc07c10c3c457a5d59ae5b5`, produzione su `788eecf280e0bb6eb22424669b89ed7ad0abc432`, Redis Free/Oregon con persistence off. Non assumere che tale stato sia ancora corrente senza una nuova lettura.

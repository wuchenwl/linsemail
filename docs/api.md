# API guide

[简体中文](api.zh-CN.md) · [Project introduction](../README.en.md)

Base URL: `http://127.0.0.1:8100/api/v1`. Interactive reference: `/api/docs`. The runtime `/api/v1/openapi.json` defines exact request/response schemas and constraints. The UI locale changes display copy; machine field names, enums and IDs remain stable. Backend error details may be Chinese; clients should branch on `code`, not translated text.

## Authentication and writes

Scripts send `Authorization: Bearer WORKSPACE_TOKEN`, using the locally generated token in `.runtime/admin.local.json`. Browser sessions use cookies and require `X-CSRF-Token` for writes. Only the local environment should expose that session bootstrap.

Mailbox access tokens are issued by `POST /mailboxes/{mailbox_id}/tokens` and can be limited to read, fetch, wait and event scopes for one mailbox. The interface generates a one-hour token and shows it only once. It does not expose provider platform credentials.

Mailbox creation and persistent wait creation require an `Idempotency-Key` of 8–128 characters. Repeating the same key and body on the same route returns the recorded response. A changed body with the same key returns 409 `idempotency_conflict`. Do not use a new key as an automatic retry after an uncertain upstream create: it may allocate another mailbox.

## Resource map

| Action | Method and path |
| --- | --- |
| Health and worker heartbeat | `GET /health` |
| Providers and capability metadata | `GET /providers` |
| Encrypted provider configuration | `PUT /providers/{provider_id}/configuration` |
| Create / list / inspect mailboxes | `POST /mailboxes`, `GET /mailboxes`, `GET /mailboxes/{mailbox_id}` |
| Queue local removal | `DELETE /mailboxes/{mailbox_id}` |
| Queue an immediate sync | `POST /mailboxes/{mailbox_id}/syncs` |
| List / inspect messages | `GET /mailboxes/{mailbox_id}/messages`, `GET /mailboxes/{mailbox_id}/messages/{message_id}` |
| Queue missing-body retrieval | `POST /mailboxes/{mailbox_id}/messages/{message_id}/fetches` |
| Inspect asynchronous operation | `GET /operations/{operation_id}` |
| Create / inspect / cancel waits | `POST /mailboxes/{mailbox_id}/waits`, `GET /waits/{wait_id}`, `DELETE /waits/{wait_id}` |
| Workspace / mailbox events | `GET /events`, `GET /mailboxes/{mailbox_id}/events` |

## Create, then observe

```json
{
  "provider_id": "grabmail",
  "name": "Product QA",
  "address_prefix": "qa-",
  "ttl_seconds": 3600,
  "labels": ["staging"]
}
```

`ttl_seconds` is 300–86400. `address_prefix` is optional; omitted, null, blank or spaces means random. A custom prefix is at most 48 ASCII letters, digits or hyphens and must start with a letter or digit. A 16-hex-character suffix is always appended. Check `capabilities.custom_prefix` before requesting one: unsupported providers return 422 `custom_prefix_unsupported`.

Creation responds 202 with an operation resource containing `id`, `state` and `resource_id`. Poll `GET /operations/OPERATION_ID` until `succeeded`, `failed` or `uncertain`. Inspect `GET /mailboxes/MAILBOX_ID` for the resulting address. An operation in `uncertain` state is not safe to assume failed at the provider.

## List and read

Lists use opaque, resource-scoped cursors with `items`, `has_more` and `next_cursor`. Do not reuse a cursor for another mailbox or list. Message records include `ingest_seq`, `body_state`, identity strength and local first-seen time. The mailbox sync object reports freshness, errors and list completeness separately from message count.

Message detail provides plain-text body, extraction candidates and their source spans. A metadata-only message may require an explicit `/fetches` operation. Upstream text is content, not executable HTML.

## Wait for new mail

```json
{
  "timeout_seconds": 120,
  "predicate": {
    "from_domain": "example.com",
    "subject_contains": "verification"
  }
}
```

Timeout is 5–300 seconds. Optional `after_seq` selects the starting watermark; otherwise the current mailbox sequence is used. Domain equality and subject/text containment are combined when supplied. Existing messages below the watermark cannot trigger the task.

Creation returns 201 and a persistent wait resource. `GET /waits/WAIT_ID` supports bounded long polling through `timeout_seconds`; a matched resource contains `matched_message_id`. Other terminal states include `cancelled`, `timed_out` and `mailbox_expired`. Cancel with DELETE. Closing the browser does not cancel the task.

## Events and errors

SSE endpoints deliver resource changes; use their event IDs for reconnects. See the OpenAPI description for cursor behavior. Resource reads remain authoritative after an event notification. Queue operations and waits have separate IDs and terminal states.

Errors use `application/problem+json` with `status`, `code`, `detail` and `request_id`. Typical codes include `validation_failed`, `provider_credential_required`, `custom_prefix_unsupported`, `mailbox_expired`, `idempotency_conflict` and `workspace_limit`. Upstream failures remain visible on mailbox sync or operation state.

## Local capture

Create a mailbox with `provider_id: "local"`, wait for it to become active and send a test message to its assigned `@local.linsemail.test` address through SMTP at `127.0.0.1:1025`. Only active local recipients are accepted. No internet MX or public SMTP delivery is configured by this feature.

# Architecture

[简体中文](architecture.zh-CN.md) · [Project introduction](../README.en.md)

Linsemail is a local control plane for temporary receiving addresses. The database is the authority for mailbox state, ingestion order, operations and waits; the browser is a view over that state.

## Components and boundaries

| Component | Responsibility |
| --- | --- |
| React + TypeScript + TanStack Query | Responsive mailbox, message, task, provider and API views |
| `frontend/src/i18n.ts` | Explicit interface translations; persisted locale and cross-tab preference updates |
| FastAPI | Validated `/api/v1` resources, authentication, idempotency and event delivery |
| SQLAlchemy + PostgreSQL | Durable records, workspace serialization and worker leases |
| Worker | Execute queued operations, poll original mailboxes, ingest messages and resolve waits |
| Registry and adapter families | Advertise implemented capabilities and isolate upstream protocols |
| Local SMTP listener | Capture mail addressed to active local test boxes |

Provider configuration is distinct from mailbox session state. Removing a platform key blocks new creations while an existing mailbox retains its own saved session. The bridge materializes a session in a temporary directory for one operation, verifies mailbox identity and encrypts the updated session before saving it.

## Receiving lifecycle

1. Validate the request and provider admission state. A write idempotency key identifies the request body within the workspace and route.
2. Commit a creating mailbox, sync job and queued operation. An HTTP 202 response is acceptance, not upstream success.
3. A worker claims a leased job. Lease ownership and epoch are checked before committing results, so an outdated worker cannot overwrite a newer result.
4. Create or reopen the upstream mailbox. Preserve the address and session. Only list-observed IDs can be used for body retrieval.
5. Normalize messages, deduplicate upstream identity, allocate a mailbox ingestion sequence and persist extraction candidates with source spans.
6. Match eligible wait tasks against new sequence numbers and emit durable resource events. The UI refreshes from the same API that scripts call.

Provider-wide budgets and backoff prevent a slow or failing source from becoming an unrestricted polling loop. Upstream list completeness, missing bodies and mailbox identity changes are errors or explicit states, rather than fabricated empty successes.

## Wait and extraction semantics

Wait predicates support sender-domain equality and subject/text containment. Unless supplied explicitly, `after_seq` starts at the mailbox’s current sequence. A body predicate cannot match an unavailable body. Waits persist in the database and transition to matched, cancelled, timed-out or mailbox-expired states.

Numeric codes require contextual text such as “verification code” or “OTP.” Link candidates are based on verification-related URL wording. Results retain source spans, rule identifiers and an ambiguity state. These heuristics produce inspectable candidates; they do not establish authenticity or automatically activate a link.

## Local trust boundary

The API binds to loopback, restricts Host/Origin and checks CSRF on browser-session writes. Workspace tokens and mailbox tokens have distinct permissions. Mail is rendered as text; remote message scripts and tracking images are not loaded.

Provider credentials and sessions use Fernet encryption. The encryption key is stored beside the local database configuration in `.runtime/`, so encryption does not protect against an attacker who can read the entire runtime directory. The default local-session mode and process launcher target a trusted workstation, not an internet-facing multi-tenant deployment.

At local mailbox expiry, pickup stops. Stored mail is purged after a 15-minute grace period. Upstream retention is independent; local removal does not promise deletion at the provider.

## Manual setup

The automated launcher is Windows-specific. To use an existing PostgreSQL instance on Linux/macOS, install Python 3.11+, Node.js 22.12+ and a PostgreSQL server, then create a database you control:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.lock.txt
npm ci --prefix frontend
npm run build --prefix frontend

# Replace USER and PASSWORD; use a database dedicated to this application.
export LINSEMAIL_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/linsemail'
python -m backend.api
```

Once the API initializes the schema, open a second terminal, activate the same environment, set the same database URL and runtime path, then run `python -m backend.worker`. The console is served at `http://127.0.0.1:8100`. This manual procedure describes the supported process topology; the release was exercised with the Windows launcher, not with a separate Linux/macOS installation.

Environment variables are listed in [`.env.example`](../.env.example). Apply them to the process environment; copying that file to `.env` does not load it automatically. Keep API and worker settings consistent. Back up the database and encryption key together if you need to preserve mailbox sessions.

## Repository map

```text
backend/                 API, database, worker and provider adapter families
frontend/                React interface, translations and frontend lockfile
data/providers.json      Provider catalog; runtime registry derives capabilities
scripts/                 Launch, language tests, screenshots and source packaging
tests/                   Legacy compatibility and protocol tests with safe fixtures
docs/                    Bilingual guides and synthetic screenshots
.github/                 CI, issue forms and pull request template
```

The optional root-level `app.py`, `mail_api.py`, `tasklet_client.py` and `openapi.json` retain the earlier catalog/mailbox API for compatibility. They are not the `/api/v1` console. Historical research captures and local execution ledgers are excluded from source archives.

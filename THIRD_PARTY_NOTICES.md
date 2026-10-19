# Third-party notices

The [MIT license](LICENSE) covers this repository’s own code. Dependencies and upstream services retain their own licenses, attribution requirements and terms. No ownership of external provider names, domains or infrastructure is claimed.

## Direct dependencies

| Component | Purpose | Upstream license |
| --- | --- | --- |
| React / React DOM | Interface runtime | MIT |
| TanStack Query | Server-state queries and cache | MIT |
| Lucide | Interface icons | ISC |
| Vite / Vite React plugin | Frontend tooling | MIT |
| TypeScript | Type checking | Apache-2.0 |
| FastAPI / Starlette / Uvicorn | HTTP API and ASGI | MIT / BSD-3-Clause / BSD-3-Clause |
| SQLAlchemy | Database access | MIT |
| Psycopg | PostgreSQL driver | LGPL-3.0-or-later |
| cryptography | Local credential encryption | Apache-2.0 OR BSD-3-Clause |
| HTTPX | HTTP client and test client | BSD-3-Clause |
| aiosmtpd | Local SMTP capture | Apache-2.0 |
| pytest | Backend test runner | MIT |
| embedded-postgres | Local PostgreSQL launcher | MIT |
| PostgreSQL | Local database | PostgreSQL License |
| Playwright (optional) | Screenshot and browser checks | Apache-2.0 |

Exact versions are recorded in the Python requirements lock and npm lockfiles. These archives distribute project source and lockfiles; dependencies, browser binaries, PostgreSQL binaries and downloaded provider frontend assets are not bundled. Consult each installed dependency’s license, including transitive packages, when redistributing binaries or built dependencies.

Provider integrations are HTTP clients. The project’s license does not grant permission to rehost an upstream service, remove attribution or use credentials you do not control. For example, Mail.tm’s service terms and attribution requirements remain applicable independently of this repository’s license.

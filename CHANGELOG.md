# Changelog

## 0.3.1 — 2026-10-19

- Upgrade cryptography, Starlette, FastAPI, pytest, Vite and the React Vite plugin to patched compatible versions. Regenerate Python and npm locks.
- Audit Python requirements and both npm dependency trees; add dependency auditing to CI.
- Preserve mailbox APIs, stored Fernet sessions, 97 provider adapters and the bilingual workspace.
- Include README badges and LINUX DO acknowledgements; remove the community promotion draft from current sources.

## 0.3.0 — 2026-10-19

- Add Simplified Chinese and English interface selection on the welcome screen and in the workspace header. Persist the language preference; localize navigation, forms, states, provider descriptions, errors, time and duration formatting.
- Retain the monochrome light/dark interface and responsive three-pane mailbox workspace.
- Add optional `address_prefix` to the unified mailbox API and creation form. Five provider protocols support a custom prefix plus 16 random hex characters; blank values use a random local part.
- Publish bilingual introductions, architecture and API guides with reproducible screenshots of every main page.
- Prepare MIT licensing, contribution and security policies, GitHub templates, CI configuration and clean source archive tooling.
- Preserve the 127-source catalog and 97 implemented external adapters. At this checkpoint, 61 need no platform key, 34 require platform credentials, two require a self-hosted deployment, and 30 remain unimplemented. Integration and current delivery availability are separate measures.

Earlier local research checkpoints are not included in the public source archives. The legacy catalog and mailbox API remain available for compatibility; the console uses `/api/v1` on port 8100.

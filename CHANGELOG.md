# Changelog

Atlas follows `0.x.y` versions until 1.0: `x` for a release with new capabilities, `y` for a fix-only release. Every deployment of the guest instance is a version, tagged `vX.Y.Z` on `main`.

## Unreleased

- 0.7: project mode for testers, integrated in the Atlas interface.

## 0.6.1 (2026-10-29)

- Guest chat: progress line in the conversation language without internal deadlines; interrupted answers show a retry button instead of a frozen spinner; composer and file chips localized, file sizes with units; fewer message actions; neutral user bubble; Atlas version under the composer.
- MCP connectors code (shared and personal connectors, per-member OAuth, three-layer allowance, approval of writes and personal calls in shared projects); not enabled on the guest instance and without interface yet (planned for 0.8). API-key calls never reach connectors.
- Operations: deployments wait until no conversation is running; the health watch no longer calls the model.

## 0.6.0 (2026-10-28)

The guest instance as deployed on 2026-10-28.

- Atlas API keys: OpenAI-compatible endpoint `/api/atlas/v1` with the `atlas` assistant model and the raw model profiles, scopes, expiry, per-key daily request and spending caps, usage endpoint. Clean API answers: steps and sources travel in a separate `atlas` field.
- API keys in the interface: every user creates, lists and revokes their own keys from the account menu; the secret is shown once with a copy button; administrators see and revoke all keys of the organization.
- Private user memory for ordinary chats: remember, recall, edit and forget, with a settings dialog; it never enters a project.
- Shared projects (back end): roles, private conversations by default, connector allowance per member.
- Approved skills with turn-local procedures.
- Guest interface: settings, memory and appearance aligned with the native style and fully localized (French, English, German, Spanish, Italian); theme-aware logo; no floating text-size bubble; localized instance notice.
- Fix: malformed generated SVG diagrams are repaired before display.

## 0.5.0 (2026-10-27)

First versioned release, the guest instance as deployed on 2026-10-27.

- Chat with web search and cited sources, document reading (PDF, Word, spreadsheets, presentations) and file production, image understanding and on-demand image generation.
- Places around the user with consent (OpenStreetMap, opening hours).
- Person lookup that names distinct people sharing a name.
- Atlas symbol and logotype across the interface.
- Developer API (raw code model) and interim Open WebUI keys for a tester group on the guest instance.
- Project mode (projects, Work on files and code, remote execution targets) available on the project instance only.

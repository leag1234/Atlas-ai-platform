# Open WebUI 0.13.3: compatibility and switch procedure

Status (2026-11-14): Atlas runs unchanged on Open WebUI v0.11.3 (pinned, production and
staging) and on Open WebUI v0.13.3 (comparison instances). The pin in
`services/orchestrator/serving.py` and in `infra/docker/Dockerfile.webui` stays on v0.11.3
until the owner decides the switch. Owner decision of 2026-11-13: Atlas follows the latest
Open WebUI release, so that Open WebUI and OpenAtelierUI share one structure.

Images:

| Release | Image reference |
|---|---|
| v0.11.3 (pinned) | `ghcr.io/open-webui/open-webui:v0.11.3@sha256:41daa0cf2561a5d4c8d1ff31ee2a98d93ab4d3ac2605cac69366ff6a3374a933` |
| v0.13.3 | `ghcr.io/open-webui/open-webui:v0.13.3@sha256:46b59158e6558eeb980bc828e0f1b30238ad24d82ed84d5e5eda1dab2a082f14` |

## What changed in Atlas for 0.13

The UI layer detects structure, never a version string.

- File chip in the composer: 0.11 nests the remove control inside the file button, 0.13 puts
  the button and the remove control side by side in a hover group. `guest-chat.js` marks the
  group as `.atlas-file-chip` and the button as `.atlas-file-face`; the composer padding in
  `chat-ui.css` targets the face.
- Answer action row: 0.13 renamed the fork action in French, Spanish and Italian. The
  matcher in `guest-chat.js` accepts the labels of both releases.
- Sidebar rows: 0.11 marks the selected link (`a.selected`, id `sidebar-chat-item`); 0.13
  wraps the link in a row element that carries `selected` and the id. The selected style and
  the row shape apply to either.
- Chat title on large screens: 0.13 hides the header row and shows the title as the selected
  tab of the workspace tab bar, which lives in `#controls-container`. Atlas still hides the
  old chat controls panel, but no longer the container when it holds the tab bar
  (`[data-panel-tab-bar]`). The title capitalisation and word-boundary cut apply to the tab.
- Title width: 0.13 sets font properties that the `font` shorthand cannot serialise (it reads
  as an empty string), so the title cut rebuilds the canvas font from the longhands.
- Settings translations (`settings-ui.js`): the Open WebUI i18n store was imported from a
  0.11.3 build chunk by its hashed file name, which does not exist in 0.13.3 (the whole
  module failed: no translation overrides, no stable menu filter). The store is now found by
  shape in the static imports of the app entry and of the root layout, the menu filter no
  longer depends on it, and
  the overrides are applied again when 0.13 replaces whole bundles.
- Message spacing: the top padding rule targets the conversation wrapper by position, not by
  the `pt-18` utility class (0.13: `pt-4`).
- Native artifacts panel: Atlas's side view replaces it. 0.11 shows it in the hidden chat
  controls; 0.13 opens it as a workspace tab beside the chat. `infra/chat-ui.env` sets
  `UI_VISIBILITY={"chat.artifacts":false}` (Open WebUI 0.12.4 and later; v0.11.3 has no such
  setting and ignores it). When the Atlas side view is open, the 0.13 tab bar stays visible.
- Voice button: matched by `#voice-input-button` as well as its label (0.13 translates it).
- Chat requests: `ChatRequest` maps OpenAI `{"type": "file"}` parts to `documents`, as the
  chat adapter already did. Both releases send attached documents this way through the Atlas
  filter; any direct validation of an Open WebUI request now accepts them.

## Remaining visible differences on 0.13.3 (owner decision, not changed)

- New native controls stay visible to users: notifications bell in the sidebar header,
  split-screen button at the right of the tab bar, "Tabs" workspace on large screens.
- The chat menu ("...") sits on the title tab instead of the header row.
- The native "Preview" view of code blocks (`chat.code_preview`) is not hidden; Atlas's side
  view chip stays the primary path.
- Admins see every native control (`UI_VISIBILITY` only applies to non-admin users).

## Upstream changes that matter to Atlas (v0.11.4 to v0.13.3)

Release notes: https://github.com/open-webui/open-webui/releases (one page per tag,
`/tag/vX.Y.Z`).

- v0.13.1, "Preload files" off by default with native function calling: attached files are
  no longer added to the conversation by Open WebUI
  (https://github.com/open-webui/open-webui/pull/33815). Measured: the Atlas filter still
  receives `files` and sends the documents as file parts on 0.13.3.
- v0.13.1: memory, skills and knowledge lists arrive as hidden tool results after the user
  message (https://github.com/open-webui/open-webui/issues/33852). Atlas disables memories.
- v0.13.0: chat controls moved into a model controls menu next to the input; tabbed
  workspace and side chats (https://github.com/open-webui/open-webui/commit/2d7cf5ec99c3fa5c7ee48b91823fb8021b846cf3).
- v0.12.4: saving a model no longer changes its sharing; `POST /api/v1/models/model/update`
  ignores `access_grants`, `POST /api/v1/models/create` still applies them
  (https://github.com/open-webui/open-webui/commit/5f95008abdfbbbfbe5c080de0347deb70eb507b6).
  Atlas creates its models with grants, so a new instance is correct; an existing model
  keeps the grants it has.
- v0.12.4: Docker image on Python 3.12 / Debian 13
  (https://github.com/open-webui/open-webui/pull/32861). The Atlas filter only uses the
  standard library, aiohttp and Open WebUI modules.
- v0.12.4: with Direct Integrations off, user-saved tool servers and terminal connections are
  not used (https://github.com/open-webui/open-webui/pull/32996). Atlas configures its
  terminal through `TERMINAL_SERVER_CONNECTIONS`: check the terminal on staging after the
  switch.
- v0.12.0: `ENABLE_PLUGINS=false` overrides the new `ENABLE_FUNCTIONS`
  (https://github.com/open-webui/open-webui/commit/f50f9e6252209760d0b96f090766e91fb826ecba).
  Atlas needs functions on (its global filter); both are on by default.
- v0.12.0: `ENABLE_NOTES=False` also refuses the notes API
  (https://github.com/open-webui/open-webui/commit/83acd08697d2985e3da03b20e3c8119c958d32d7).
- v0.12.0: `DEFAULT_LOCALE` handling fixed after a v0.11.4 regression
  (https://github.com/open-webui/open-webui/pull/31551). Atlas sets it empty.
- v0.11.4: `X-OpenWebUI-Auth-Type` header on requests to OpenAI connections
  (https://github.com/open-webui/open-webui/commit/ee46e2664ab23bacc536fed21c06ab19067d695b).
- v0.11.4: some Python packages removed from the image (langchain-community, nltk and others,
  https://github.com/open-webui/open-webui/pull/29725). Atlas does not use them.
- Database: v0.12.0, v0.12.1, v0.13.0 and v0.13.1 change the schema; the notes ask for a
  backup before upgrading and for all instances to be updated together. No downgrade path is
  documented.

## Data migration test (copy of staging UI data)

Run on 2026-11-14 on the build VM, comparison instance only (port base 27200): a read-only
SQLite backup of the staging UI database plus a copy of its uploads, Atlas from this branch in
stable mode with project mode, Open WebUI v0.13.3, synthetic provider transport (0 EUR). The
same harness was run with v0.11.3 on the same copy as the reference.

- Open WebUI migrates the database itself on the first start (alembic `d4c1a8e37b62` to
  `9f6c2a8d1b43`, 43 to 53 tables). The new tables are `app`, `auth_session`,
  `chat_view_state`, `comment`, `comment_reaction`, `function_history`, `model_history`,
  `notification`, `skill_history` and `tool_history`; no table is removed.
- Row counts are identical before and after for users, auth, chats, chat messages, files,
  folders, functions, models, prompts, tags, feedback, API keys, groups and access grants.
  SQLite integrity check: ok.
- Atlas configures the instance without workaround (ready in 18 to 26 s over three runs) and
  the UI comes back 15 to 47 s after a container restart.
- Browser checks in light, dark and phone views, before and after the restart: sign-in through
  the trusted-header relay, home page, Atlas elements (beta badge, version footer, scripts),
  opening an existing conversation, project list, API keys entry in the account menu, a new
  chat turn, the project view from the sidebar, and an attachment turn (chip with its remove
  control, document text reaching the model call). All pass on both releases (final run:
  37 of 43 checks on each release, the 6 others being the pinned-chat case below).
- One check differs from the database on both releases alike: the chat list API leaves out
  pinned chats by design (one pinned chat in the copy).
- Open WebUI v0.11.3 started on the migrated copy fails at start-up (alembic cannot locate
  the new revision). The migration is one way.

## Switch procedure (owner decision required; staging first, then production)

The procedure reuses the backup and rollback discipline of the earlier front-end cutover
tooling on the build VM (`/opt/atlas-src/tools/oau_cutover.sh`, `oau_rollback.sh`). There is
no conversion step: Open WebUI migrates its own database at start-up.

1. Merge the compatibility change and deploy it on staging with v0.11.3 (no visible change).
2. Pull the image by digest on the VM:
   `docker pull ghcr.io/open-webui/open-webui:v0.13.3@sha256:46b59158e6558eeb980bc828e0f1b30238ad24d82ed84d5e5eda1dab2a082f14`.
3. Wait for an idle instance (no chat or API activity for 5 minutes, `tools/guest_wait_idle.sh`),
   pause the instance watchdog, stop the unit (`atlas-staging`, then later `atlas-stable`).
4. Back up the UI volume (`atlas-chat-ui-200` for staging, `atlas-chat-ui-100` for production)
   into a new volume `atlas-chat-ui-<base>-pre-owui0133-<stamp>` with `cp -a`, and verify the
   file count, bytes and checksums (as `oau_cutover.sh` does). Also keep a SQLite backup copy
   of `webui.db`.
5. Add one line to the instance env file (`/opt/atlas-src/staging.env`, later `stable.env`):
   `ATLAS_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.13.3@sha256:46b59158e6558eeb980bc828e0f1b30238ad24d82ed84d5e5eda1dab2a082f14`.
   Check the file still sources: `bash -c "set -a; . /opt/atlas-src/staging.env"`.
   No other environment change is required (`UI_VISIBILITY` already ships in
   `infra/chat-ui.env`).
6. Start the unit. Open WebUI migrates the database on its first start. Check the health
   endpoint, then the UI container image (`docker inspect -f '{{.Config.Image}}'`).
7. Acceptance on the instance: the real smoke battery (web search, document, image, API key,
   project turn), the dual front-end browser journey, the terminal connection, and the
   owner's UI flows (menus, move, search, dialogs) in light, dark and phone views.
8. Production only after staging has passed and with the owner's go, same steps.
9. Switch the pin for new installations in a follow-up change: `WEBUI_IMAGE` default in
   `services/orchestrator/serving.py` and the `FROM` line of `infra/docker/Dockerfile.webui`.

Rollback: stop the unit, restore the backup volume contents into the UI volume, remove the
`ATLAS_WEBUI_IMAGE` line, start. The migrated database is not usable by v0.11.3
(measured: start-up fails on the unknown alembic revision), so a rollback always restores the
backup; chats written after the switch
are lost in a rollback.

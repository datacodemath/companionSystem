# Antigravity standalone GET repair — 2026-11-21

Request: repair Companions blocking Antigravity. Inbox evidence:
`9cf147a9-8bc8-4e93-9e3b-4288635ea644`, Yoga manual ticket 36. Yoga reported
CLI 1.5.14 authenticated but retrying a standalone SSE stream five times,
without exposing tools. We independently confirmed the deployed GET handler
returned one SSE comment and closed the connection.

Authenticated GET now returns empty HTTP 405 with `Allow: POST`, after the
existing origin, authentication and protocol-version checks. POST JSON/SSE,
OAuth and notification handling are retained. This follows the optional GET
stream behavior in the [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Deployment and recovery

Actual previous MCP image: `profile-os-mcp:closeout-search-20261114`.
The workstation checkout is older; deployment uses the exact running source,
changing only the GET branch. The derivative image replaces only that module.

- New MCP image: `profile-os-mcp:agy-get-20261121`, healthy.
- Backend retained: `profile-os-backend:closeout-search-20261114`, healthy.
- Before source SHA256: `2b544d1842a48880db022a52dd897ee6fe4d1feb99433c2c0d3e917d2d8c825b`.
- After source SHA256: `e2fa5f6119b28c3fde47db8197b7522ae574ab8978318bb12891f127a9447020`.
- VPS release: `/opt/profile-os/releases/agy-get-20261121/`.
- Release contains original/patched source, Dockerfile, verification/deployment
  scripts, MCP-only overrides, exact deploy/rollback commands in `commands.json`,
  and `live-verification.json`. No credentials copied to workstation or receipts.

To roll back, execute the `compose.rollback.json` command array saved in
`commands.json` from `/opt/profile-os`. It retains the preceding compose
configuration and recreates only MCP. Do not rerun deploy.py: its guard
intentionally requires the previous image. Future deployments must preserve
this overlay or incorporate this GET repair into newer pinned source.

## Verification

- 49 MCP/protocol/OAuth tests passed; 9 bridge tests passed.
- Sandbox TestClient diagnostic stalled; documented diagnostic outside sandbox
  returned `200 4`; escalated test runs passed. No test-client changes.
- Candidate image, isolated network: initialize, initialized notification,
  GET 405, tools/list, auth/origin/version failures passed for all three
  supported protocol versions and both POST response formats.
- Public HTTPS after deployment: same matrix passed, 30 tools exposed;
  `summon_companion(limo)` succeeded with JSON and SSE POST responses.
- Existing ChatGPT connector `list_files(hilo)` returned successfully afterward.
- Yoga `agy mcp list` confirms enabled native HTTP connector at the expected URL.

Andrés independently tested from Yoga and confirmed recovery: "that worked I
tested myseflf from yoga". This is user-confirmed client acceptance, following
the independently verified server checks. Earlier automatic approval review
rejected the agent's Antigravity status command because it might start a model
turn; no workaround or model job was attempted.

## Repository reconciliation at requested commit

Andrés requested committing/pushing with the most recent production version.
Recovered code only from the actual running containers: backend owns all
application modules except `profile_os/mcp_server.py`, which comes from MCP.
The two images contain different historical copies of unused service modules;
the repository now combines their owning copies. Every selected source file
matches production byte for byte; see `production-source.json` for hashes.
No runtime data, private profile files, hydration packets or environment files
were retrieved. No additional production changes were made.

This recovers previously deployed atomic inbox reads, shared shelf, symbol
signatures, invocation modes and historical closeout search. Recovered their
release tests while preserving local browser-auth, loopback OAuth and shared
question regression coverage. Full consolidated suite: **355 passed in 17.04s**.
The full suite ran outside the sandbox as required by the verified diagnostic.

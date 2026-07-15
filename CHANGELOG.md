# Changelog

All notable changes to the IICP Python SDK (`iicp-client`).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
within the scope of the IICP Software axis (see [`VERSIONING.md`](https://github.com/RobLe3/iicp.network/blob/main/project/VERSIONING.md)
in the main repo).

## [Unreleased]

## [0.7.91] — 2026-07-15

### Fixed — cross-SDK route projection parity
- Chat model, QoS, region, and reputation requirements now survive ticketed discovery, legacy discovery, and fallback through one canonical route projection.
- Route-only criteria no longer leak into provider execution constraints; shared transcript fixtures keep Python, TypeScript, and Rust behaviour aligned.
- Consumer authentication can be made fail-closed with `consumer_auth_mode=required` while the adoption-friendly default remains optional.

## [0.7.90] — 2026-07-14

### Added — relay abuse resistance and encrypted responses
- Relay binds are rate-limited per source principal without logging raw source addresses; recovery of a dead bound session remains exempt.
- Nodes advertise and negotiate `response_encryption_v1`; direct and relay-routed tasks return authenticated encrypted responses when explicitly negotiated, while downlevel peers retain Tier-1 compatibility.

## [0.7.89] — 2026-07-14

### Fixed — coordinated Quick Tunnel recovery
- Shared Quick Tunnel capacity is now waited out inside the tunnel opener, with a bounded randomized suffix after each host-wide or provider deadline. Simultaneously waking local nodes no longer convert ordinary capacity pacing into synchronized supervisor restarts.

## [0.7.88] — 2026-07-13

### Added — MeshLLM provider compatibility
- `iicp-node serve --backend-type meshllm` uses MeshLLM's local OpenAI-compatible gateway at `http://localhost:9337/v1` for stable chat capability.
- Readiness requires both MeshLLM `/readyz` and the selected model in `/v1/models`; the experimental `mesh` ensemble remains explicit opt-in.

### Fixed — relay recovery evidence
- A successfully bound relay worker is treated as a live public route while directory evidence converges; a disconnected worker returns to normal limited-reach recovery.

## [0.7.87] — 2026-07-11

### Added — operator data-rights self-service
- `iicp-node operator dsr export --output FILE` requests a short-lived directory challenge, signs it locally with the existing operator identity, and writes a redacted, owner-only receipt (mode `0600` on Unix).
- `iicp-node operator dsr restrict --yes` and `iicp-node operator dsr anonymize --yes` require explicit confirmation and report the directory retention boundary; they never transmit the operator secret or node tokens.
- Docker release coverage now verifies the signed, redacted DSR flow against a fake directory for Python, TypeScript, and Rust alongside existing registration and recovery checks.

## [0.7.86] — 2026-07-10

### Added — operator-signed node policy publishing
- `iicp-node serve --policy-manifest FILE` signs a local public policy document with the existing operator identity and advertises it on every registration and recovery re-registration.
- Canonical JSON and Ed25519 output share a fixed known-answer vector with the TypeScript, Rust, and directory verifier tests.
- Relay operator guidance now documents the available strict directory-signed bind-ticket cutover instead of describing bind authentication as pending.

## [0.7.85] — 2026-07-10

### Security — one-use relay bind tickets
- Directory-signed relay bind tickets now require a signed 128-bit `jti`; relay accept paths atomically consume it and reject replay after disconnect until ticket expiry.
- Replay protection covers native RELAY_BIND and browser-compatible HTTP-poll binds without changing the opt-in strict-bind cutover.

## [0.7.84] — 2026-07-10

### Added — MCP safety controls and operator self-service signing
- The packaged MCP gateway now denies all named non-benign risk classes unless explicit operator opt-in, caller authorization policy, an accepted sandbox profile, and redacted audit are configured together.
- Gateway registration now uses the current directory `capabilities`/`limits` contract and advertises only tools that passed the local risk gate.
- Added a canonical operator-key signing helper for challenge-based acceptance and DSR self-service requests.
- Added a packaged canonical MCP risk taxonomy and redacted policy receipts that exclude tool arguments.

## [0.7.83] — 2026-07-09

### Added — policy-manifest identity routing
- Added `required_manifest_identity_level` to routing policies so strict callers can require `signed_valid`, `operator_bound`, or `known_operator` node policy manifests before any prompt is dispatched.
- Rejected or rotated/revoked manifests fail closed with redacted routing-policy reasons and no task POST to the rejected node.
- Expanded the packaged `mcp-gateway` dangerous-tool denylist for public-unknown callers and redacted upstream MCP error details to avoid echoing tool-input or secret text.
- Prefer short-lived ticketed route discovery and fall back only when an older directory explicitly lacks the endpoint.
- Refuse prohibited and high-risk declared intents locally from the packaged canonical risk taxonomy.
- Mark model responses as AI-generated and add the compatibility-proxy response header without changing standard response bodies.

## [0.7.82] — 2026-07-09

### Fixed — saved credential recovery
- Successful provider re-registration now persists refreshed `node_token` and `node_hmac_key` back to the saved node identity, so read-only commands such as `iicp-node credits` do not drift behind a healthy running node.
- `iicp-node doctor` now reports stale saved credentials separately from serving health and explains `backend_cold` as normal idle/warmup rather than an automatic restart condition.

## [0.7.81] — 2026-07-02

### Fixed — pipx self-updater bootstrap
- The background provider self-updater now bootstraps `pip` with stdlib
  `ensurepip` when the active interpreter has no `pip` (common for pipx app
  venvs and `python -m venv --without-pip`), so hourly upgrades no longer
  silently no-op forever.
- Updater failures now preserve a heartbeat-visible `sdk_update_error_class`
  such as `ensurepip_failed`, `pip_missing`, or `pip_upgrade_failed`, making
  downlevel drift diagnosable from the directory.

## [0.7.80] — 2026-07-02

### Added — remote-routing policy profiles
- Added client-side routing policies that filter discovered nodes before any prompt is dispatched: `standard`, `sensitive`, `eu_restricted`, `strict_policy`, and `debug_override`.
- `iicp-node query` now exposes `--routing-profile`, `--region-allowlist`, and `--allow-remote-executor`, and prints a plain privacy reminder that remote executors can read prompts they run.

## [0.7.79] — 2026-07-02

### Fixed — direct IPv6 route promotion recovery
- Provider heartbeat recovery now treats `http_ipv6` + self-attested + browser-unusable registry evidence as limited reach, so supervised nodes restart and retry Quick Tunnel or relay fallback instead of staying indefinitely in “Direct IPv6 — unverified”.

## [0.7.78] — 2026-07-01

### Fixed — relay-capable public fallback guard
- Relay-capable nodes no longer fall back through another relay when their own public tunnel is cooling down or unavailable. They preserve relay capacity and, under supervision, fail visibly so launchd/systemd/Docker can retry the public route.
- Automatic relay election now excludes the node's own `node_id`, avoiding self-election loops during transient Quick Tunnel rate-limit recovery.
- Ordinary provider nodes can still use relay fallback as the last-resort path; this guard only applies to nodes explicitly advertising relay service.

## [0.7.77] — 2026-07-01

### Added — supervised recovery and Docker release validation
- Added `iicp-node doctor` so operators can check local health, directory presence, and the deterministic recovery action without reading raw logs.
- Provider heartbeat loops now classify recovery state, re-register when directory evidence disappears, and only exit for supervisor restart after configured grace checks.
- Docker images default to `IICP_PORT=8020`, matching the exposed port and healthcheck used by low-friction container runs.
- Release validation now includes the cross-SDK Docker gate for CLI help, no-network update checks, fake-directory registration, `/iicp/health`, `/v1/task`, and heartbeat 401 recovery.

## [0.7.76] — 2026-06-30

### Changed — operator-wallet credit display
- `iicp-node credits` now shows the operator wallet summary before per-node ledgers when the directory provides `operator_wallet`, making earned and spendable credits easier to understand across multiple nodes.
- JSON output preserves per-node ledgers while exposing the pooled wallet fields for tooling and future give-and-get accounting.

## [0.7.75] — 2026-06-28

### Fixed — host-wide Quick Tunnel pacing
- Accountless Cloudflare Quick Tunnel creation now uses host-wide create spacing, a short create lease, and persistent provider-rate-limit cooldown so Dockerized or launchd-managed nodes do not retry-storm while Cloudflare limits recover.
- When tunnel creation is paced, cooling down, or held by another local node, providers fall back to the next safe reachability path instead of advertising an unverified public route.

## [0.7.73] — 2026-06-27

### Fixed — Quick Tunnel rate-limit backoff
- Accountless Cloudflare Quick Tunnel startup now opens a process-local cooldown when `cloudflared` reports rate limiting (`429` / `1015`) so retries do not hammer Cloudflare; operators should use a named tunnel or `IICP_PUBLIC_ENDPOINT` for persistent relay infrastructure.

## [0.7.72] — 2026-06-26

### Fixed — Quick Tunnel DNS-lag stability
- Quick Tunnel verification now avoids destructive tunnel rotation when local
  DNS has not resolved a freshly-created `trycloudflare.com` hostname yet but
  Cloudflare DoH already publishes the A/AAAA record.
- Startup errors now include the last Cloudflare output line, making rate-limit
  responses such as HTTP 429 visible in operator logs.

## [0.7.71] — 2026-06-26

### Fixed — supervised Quick Tunnel dead-state recovery
- Added `IICP_TUNNEL_DEAD_POLICY=auto|retry|exit|log-only` so operators can choose whether confirmed Quick Tunnel Dead state retries, exits, or only logs.
- Generated launchd/systemd units and Docker images now set `IICP_SUPERVISED=1`; default `auto` exits non-zero under a supervisor so launchd/systemd/Docker can restart instead of leaving a publicly unreachable process alive.
- Foreground/manual runs keep retrying with backoff by default, preserving a low-friction local development experience.
- Dockerfiles default to `IICP_SUPERVISED=1` and the `auto` dead policy, matching generated launchd/systemd service units.
- README and contributing docs now describe Docker restart-policy expectations, current issue trackers, and the one-time manual-upgrade caveat for nodes older than 0.7.67.

## [0.7.70] — 2026-06-25

### Added — elastic Quick Tunnel recovery
- Quick Tunnel providers now mark themselves unavailable while the public tunnel URL is in twilight/recovery and only re-register a rotated URL after public `/iicp/health` verifies.
- Added tunnel state reporting (`ready`, `twilight`, `recovering`, `dead`) so service heartbeats reflect real public reachability instead of local process liveness.

## [0.7.68] — 2026-06-25

### Fixed — fail-closed IICP-CX routing
- Consumers now skip keyless discovered nodes by default and refuse plaintext when no keyed node remains.
- Transitional plaintext requires explicit `IICP_CX_ALLOW_PLAINTEXT=1` for debugging only.

## [0.7.67] — 2026-06-25

### Fixed — unattended updater parity
- Normal `iicp-node serve` now starts the background self-updater, matching the
  service-managed and cross-SDK unattended behavior.
- Auto-update checks default to hourly and report update evidence in heartbeats.

## [0.7.66] — 2026-06-21

### Verified — discover CX key dual-field migration
- Added regression coverage for transitional directory responses that contain both canonical `cx_public_key` and deprecated `public_key`; Python already prefers `cx_public_key` and encrypts to the canonical key.
- Retains browser/routing signal parsing for directory v1.10.50+.

## [0.7.65] — 2026-06-21

### Fixed — discover CX key alias
- Consumers prefer canonical `cx_public_key` and treat a directory `public_key` field as a deprecated alias, so keyed live nodes are encrypted instead of receiving the
  transitional plaintext fallback warning.
- Added regression coverage for the alias path.

## [0.7.64] — 2026-06-20

### Changed — provider-side IICP-CX
- Provider nodes now persist an X25519 CX key locally and advertise the public half as
  `cx_public_key` during registration.
- `POST /v1/task` decrypts incoming `iicp_conf` envelopes before invoking the task handler,
  closing the missing provider-side half of mandatory payload confidentiality.
- Added regression coverage for CX key advertisement and encrypted task handling.

## [0.7.63] — 2026-06-20

### Changed
- Tier-3+ reachability now tries the node's own Quick Tunnel before electing a third-party relay; `--no-tunnel` retains relay-first behavior.
- The background updater performs its first check within five minutes of startup, then returns to the configured cadence.

### Fixed
- `--tunnel` help now describes the actual tunnel-first reachability order.
- Reachability order is produced by the same pure planner covered by unit tests.
- Added a targeted test for the updater's initial-delay rule.

## [0.7.62] — 2026-06-13

### Fixed
- **#10 (serve goes offline):** `cli.py` called `node.node_hmac_key()` but it's a `@property` →
  `'str' object is not callable` on 0.7.61, failing serve registration across all retries so the
  node never reached a registered state (heartbeats stopped, directory marked it offline) though
  the server-side register succeeded. Now read as the property attribute. Regression-tested.

### Changed — privacy-first (mandatory E2E, no opt-out)
- IICP-CX payload encryption is now **on by default with no opt-out**: the client always encrypts
  to a node advertising a `cx_public_key` (the `use_confidentiality` flag is a deprecated no-op).
  The directory, relays, and network see only ciphertext. A node not yet advertising a key gets a
  transitional plaintext warning during rollout. The executing node still decrypts to run the model
  (run locally for full privacy).
- Added Tier-2 response-encryption primitives (`encrypt_response`/`decrypt_response`) — not yet
  wired into the task flow.

## [0.7.61] — 2026-06-13

### Fixed — self-healing tunnel (resilience, #538)
- The `--tunnel` watchdog now actively health-checks the tunnel's OWN public URL (GET
  `/iicp/health` through the Cloudflare edge) every 30s, not just watch for the cloudflared
  process to exit. A Quick Tunnel can keep its process alive while its edge connection drops,
  leaving a dead public endpoint the directory still serves. After 3 consecutive unreachable
  probes the watchdog restarts cloudflared → new URL → re-register; the respawn cap resets when
  a fresh tunnel passes health, so a long-running relay self-heals indefinitely. Parity with Rust/TS.

## [0.7.60] — 2026-06-13

### Added — background self-updater (#521 P2)
- A node running `serve` now keeps itself current automatically: it periodically checks the
  registry and, when a newer release is published, `pip install --upgrade`s and re-execs onto
  the new version in covered service paths — no operator intervention. Early Docker/normal-serve
  coverage was hardened in 0.7.67, so older nodes may need one manual upgrade/restart first.
  Default-on; opt out with `IICP_AUTO_UPDATE=0`. Check cadence via
  `IICP_AUTO_UPDATE_INTERVAL_S` (default 1h, min 5m). Loop-safe (post-upgrade the running
  version equals latest) and failure-isolated (a failed upgrade never restarts or crashes the node).

### Security
- Expand the `mcp-gateway` dangerous-tool denylist backstop (red-team pass 3) — broaden the
  set of shell/exec/interpreter tool names refused by default when exposing an MCP server as a
  mesh node, reducing the chance a permissive MCP server leaks an arbitrary-execution tool.

## [0.7.59] — 2026-06-12

### Security

- **Per-Origin `/v1/task` rate limit (F4, #524)** — caps browser-origin task
  dispatch (the CORS confused-deputy vector); non-browser callers (the
  operator's own authed traffic) are never throttled. 429 IICP-E023; default
  120/60s, `IICP_TASK_RATE_LIMIT` overrides (0 disables).

### Added — re-registration ownership proof (#529)

- The node now sends `current_node_token` on re-registration when it holds a
  cached token, so an endpoint change after a tunnel/CGNAT rotation is accepted
  via the directory's IICP-E050 ownership path. Additive + backwards-compatible
  (directory accepts-but-does-not-require it).

## [0.7.58] — 2026-06-12

### Security — relay session cap (red-team F5)

- The relay caps concurrent worker sessions (default 256); new binds past the
  cap are rejected (HTTP 503 `IICP-E039` / TCP `RELAY_ACK` error), closing a
  bind-flood memory-exhaustion DoS. A rebind of an existing worker_id is exempt.

### Added — `iicp-node update --check`

- Read-only check for a newer published release (numeric version compare) with
  the exact upgrade command. Exit 10 when a newer release exists, 0 otherwise.

## [0.7.57] — 2026-06-12

### Added — automatic Quick-Tunnel escalation (NAT ladder rung 5, #520)

- When every NAT path fails (no direct endpoint, no UPnP pinhole, no IPv6
  GUA, no relay-capable peer in the directory), the node now exposes itself
  via a zero-account Cloudflare Quick Tunnel automatically: detect
  `cloudflared` on PATH (never auto-installed — one actionable install hint
  when missing), spawn it, register the issued `https://*.trycloudflare.com`
  URL as the endpoint (`transport_method=external_tunnel`), supervise the
  child (bounded respawn ×3), and tear it down with the node on every exit
  path.
- `--tunnel` forces the rung regardless of NAT tier (e.g. to get an https
  endpoint for browser consumers without touching the router);
  `--no-tunnel` / `IICP_TUNNEL=0` disables the automatic escalation.

## [0.7.56] — 2026-06-12

(Also includes the never-published 0.7.55 changes: MCP gateway as a built-in
`iicp-node mcp-gateway` feature.)

### Added — HTTP long-poll relay worker transport (#450)

- Relay-capable nodes accept browser-compatible workers over plain HTTP:
  `POST /v1/relay/bind` (bearer session token; 409 on alive-rebind, #510
  interim-C), `GET /v1/relay/pull` (long-poll ≤25 s), `POST /v1/relay/result`,
  `POST /v1/relay/unbind` — same session registry as TCP RELAY_BIND workers.
- Path-scoped worker endpoints `{relay}/v1/relay-for/<worker_id>/v1/task` +
  `/iicp/health`: published consumers route through the relay with no client
  changes. RELAY_ACK gains additive field 4 (the relay's HTTP task port).

### Fixed — relay-bound workers were silently misattributed

- Relay workers previously advertised the bare relay endpoint, so consumer
  dispatches executed **on the relay itself** instead of forwarding (and used
  the non-HTTP accept port). Workers now register the path-scoped endpoint.

### Changed — CORS on every node HTTP endpoint

- All node responses carry `Access-Control-Allow-Origin: *` and every path
  answers `OPTIONS` preflights. Web pages (e.g. iicp.network/browser-node)
  are first-class consumers: an https-exposed node now serves browser
  dispatches directly. No new capability — CORS only ever gated browsers;
  curl was never restricted.

## [0.7.54] — 2026-06-11

### Fixed — `iicp-node credits` resilience

- Transient failures (network error, 5xx, undecodable body) are retried once after
  a 2s pause — deploy windows / shared-hosting blips no longer surface as one-shot
  CLI errors (`HTTP 500` / `bad response: error decoding response body`).
- All-nodes listing (bare `iicp-node credits` with multiple saved nodes): one
  node's failure no longer aborts the whole listing — every node is shown and the
  command exits non-zero with an `N/M node(s) failed` summary.

## [0.7.53] — 2026-06-11

### Added — model-drift re-registration (#494)

- Each heartbeat tick compares the backend's live model list against the registered
  set and automatically re-registers when they diverge — directory registration no
  longer goes stale when Ollama loads/unloads models.

## [0.7.52] — 2026-06-10

### Added

- #496 Phase-2 consumer token support.
- `models[]` array on the `/iicp/health` endpoint (#494).
- #503 loud CLI notice when serving without an operator identity.

## [0.7.51] — 2026-06-10

### Added — health_models heartbeat reporting (#494)

- **`backend_url` / `backend_api_key`** in `NodeConfig` — when set, each heartbeat probes
  the backend's live model list (`/api/tags` for Ollama, `/v1/models` for OpenAI-compatible
  backends) and sends `health_models=[...]` in the heartbeat payload.
- The directory (≥ v1.10.28) uses `health_models` to filter `?model=` discover queries
  to nodes whose backend actually has that model loaded, eliminating stale-model routing.
- Probe failures are soft — heartbeat still fires without `health_models` (backward compat).
- 3 behavior tests added (`test_serve.py`).

## [0.7.40] — 2026-06-07

### Fixed — CLI usability hardening (no friction for new operators)

- **`proxy` now listed in `iicp-node --help`** + all serve flags documented.
- **Every subcommand `--help`/`-h` prints usage** instead of crashing.
- **Friendly parse errors** — unknown flags print `ERROR: …` (exit 2) instead of tracebacks.
- **`iicp-node serve --model X` works without `--backend-url`** — `localhost:11434` default applied unconditionally.
- **`--no-auto-detect-nat`** off-switch; `iicp-node help` prints usage; `credits` auto-resolves single node. Cross-flavour CLI parity (3-C).

## [0.7.39] — 2026-06-07

### Added — unified client: local OpenAI/Ollama/Anthropic-compat proxy (ADR-050, #476)

- **`iicp-node proxy`** — a local compat gateway on `127.0.0.1:9483`. Speaks OpenAI
  (`/v1/chat/completions`, `/v1/models`), Ollama (`/api/chat`, `/api/generate`, `/api/tags`),
  and Anthropic (`/v1/messages`) and routes each request across the IICP mesh.
- **`iicp-node serve --with-proxy`** — co-host the proxy next to a provider node in one process.
- **CIP consumer gating** in the proxy path — `IICP-E036` → 402, `IICP-E022` → 503.
- One client now does **node + query + proxy**; the standalone `iicp-proxy` package is retired.

## [0.7.36–0.7.38] — 2026-06-03..06

- Maintenance + lockstep version alignment across Python/TS/Rust SDKs (3-C). No API changes.

## [0.7.35] — 2026-06-03

### Added — native Anthropic backend + audio chat modality (#414, capability roadmap)

- **`backend_type="anthropic"`** — speaks the Anthropic Messages API directly; defaults
  `backend_url` to `https://api.anthropic.com`.
- **Audio modality detection** — model names containing `audio`, `voxtral`, or `omni`
  advertise `input_modalities: ["audio"]`.

### Added — heartbeat liveness challenge (ADR-047 Part A, #411)

- The heartbeat loop answers the directory's liveness challenge.

## [0.7.34] — 2026-06-03

### Added — operator delegation at registration (ADR-045 Phase A, #407)

- The node signs an ed25519 operator delegation on `register`.

## [0.7.33] — 2026-06-03

### Added — multimodal capability advertising (ADR-046, #408)

- `build_capabilities` advertises `input_modalities` (text + image for vision models).

## [0.7.32] — 2026-06-03

### Added — multi-intent advertising (#409)

- A node advertises every intent its backend serves (chat + embedding).

## [0.7.31] — 2026-06-02

### Fixed — backend_url precedence regression-lock (#410)

## [0.7.30] — 2026-06-02

### Added — Bearer auth for OpenAI-compat backends (#5)

- **`--backend-api-key` / `IICP_BACKEND_API_KEY`**.

## [0.7.29] — 2026-06-02

### Fixed — single-instance lock prevents duplicate-node thrash (#405)

- Per-node pidfile; `--force` / `IICP_FORCE` to take over.

## [0.7.28] — 2026-06-02

### Fixed — node no longer needs restart to reconnect (#404, reliability)

- Registration retries with backoff; heartbeat loop re-registers on 401.

## [0.7.27] — 2026-06-02

### Fixed — CIP policy now enforced on incoming tasks (#403, security)

- `cip_gate` rejects tool-execution-domain intents unless the operator opted in.

## [0.7.26] — 2026-06-02

### Added — transport on parsed discover nodes (#397)

- `Node.transport: list[str]` — protocols each node speaks.

## [0.7.25] — 2026-06-02

### Fixed — node recovers after the directory drops it (#399)

- Heartbeat loop re-registers on node-unknown rejection (404/401/410).

## [0.7.24] — 2026-06-02

### Changed — onboarding clarity

- `iicp-node init` distinguishes optional capabilities from real problems.

## [0.5.x] — 2026-05-27

- 0.5.3: CBOR wire-compat fix (integer keys, RFC 8949); 3×3 cross-SDK matrix verified.
- 0.5.2: ConcurrencyGate parity port (Tier 2 Item 5).
- 0.5.1: CONF self-conformance probes (Tier 2 Item 4).
- 0.5.0: ADR-019 declarative pricing + HMAC signing (Tier 2 Item 3).

## Earlier 0.x releases

See git log — the Tier 1 ports (transport_endpoint, IICP TCP, UPnP, openai_compat,
NAT observability) and Tier 2 items (CIP policy, pricing, conformance, ConcurrencyGate)
shipped across iter-1409..1440 of the main repo's FORGE loop.

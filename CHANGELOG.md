# MCP-Genelab Change Log

# v0.4.0 → v0.5.0

| | v0.4.0 | v0.5.0 |
|---|---|---|
| MCP SDK | `mcp>=1.6.0` (FastMCP, 1.x) | `mcp>=2.2.0,<3` (`MCPServer`, 2.x) |
| Tools | 22 + `plot://{filename}` resource | **24** + `plot://{session_id}/{filename}` resource |
| Deployment | local stdio / generic Docker | **ECS Fargate behind ALB + CloudFront + WAF** (shared-process, multi-user) |
| Tests | 112 tests / 9 test files | **199 tests / 11 test files** (all passing on mcp 2.2.0) |

---

## 1. Highlights

- **Public, multi-user deployment.** The server now runs as one shared process on ECS Fargate (CloudFront + WAF → ALB → Fargate → Neo4j on EC2). Everything that used to be a per-process assumption — output directory, plot registry, credentials, health, metrics — was reworked for that.
- **Application-level sessions.** New `create_session` / `end_session` tools; every other tool takes a `session_id`; the plot resource is `plot://{session_id}/{filename}`. One conversation = one session; sessions are unguessable, bounded, and expire.
- **Endpoint hardening.** Query timeouts, row caps, a forbidden-procedure filter on top of the write filter, fail-fast credentials (no baked-in password in the image), path validation, scrubbed logging, Neo4j pool limits.
- **Load-balancer health routes** `GET /healthz` and `GET /readyz`.
- **Configurable path prefix** (`MCP_PATH_PREFIX`): the MCP endpoint, health routes and `/metrics` can all be served under a prefix (e.g. `/kg` → `POST /kg/mcp`) to match how the ALB / CloudFront publish the service, since neither can rewrite paths.
- **Usage metrics** without any AWS SDK: JSON usage log, optional CloudWatch EMF metrics, optional `/metrics` endpoint; per-conversation and per-client attribution.
- **MCP Python SDK 2.x** port.
- **Routing improvements:** "what groups / control vs experimental groups are in a study" now routes to `select_assays`.

---

## 2. Breaking changes (client-visible)

1. **`session_id` is required on every tool except `create_session`** when the server runs over a remote transport (`MCP_SESSION_POLICY=strict`, the Docker default). Clients must call `create_session` once and pass the returned token on each call; omitting it is a schema-validation error, and an empty, malformed, unknown or expired one returns `Error (missing|malformed|unknown|expired session): … call create_session()` (`create_session` itself can return `Error (capacity session)`). Local stdio use is unaffected (`implicit` policy: no `session_id` needed).
2. **Plot resource URI** changed from `plot://{filename}` to `plot://{session_id}/{filename}`. `fetch_plot` and `get_save_script` take `session_id` and emit the new URI.
3. **Remote transports refuse to start without `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`** (fail-fast); the Docker image no longer contains default credentials. stdio keeps the localhost defaults.
4. **`query` tool** now also rejects `LOAD CSV` and `CALL`s to `apoc.load/export/trigger/periodic/refactor/create/merge/cypher/systemdb.*`, `dbms.*`, `db.create*`, `db.drop*`, `db.index.fulltext.create*`/`drop*` (other `db.index.fulltext.*` procedures and `apoc.meta.*`/`apoc.help` remain allowed); caps results at 1,000 rows (notice begins "⚠️ Results truncated at N rows"); cancels queries after 60 s; error messages no longer echo the Cypher or the raw exception; the rejection text changed ("This query was rejected. Only read-only graph queries are permitted…", no longer prefixed `Error:`).
5. **Tool count 22 → 24** (`create_session`, `end_session`).
6. **Runtime dependency** `mcp>=2.2.0,<3` (was `>=1.6.0`); `starlette` declared explicitly. Python floor stays 3.10.

---

## 3. New tools and changed tools

| Tool | Change |
|---|---|
| `create_session` **(new)** | Mints an opaque 43-char session id (`secrets.token_urlsafe(32)`), returns `session_id: <token>` plus the configured idle/absolute lifetimes; refuses with `Error (capacity session)` if the store is full and no idle session can be recycled. Takes an injected `Context` (no client-visible parameters). Annotations: title "Create Session (call first)", `readOnlyHint=False`, `idempotentHint=False`. |
| `end_session` **(new)** | Discards the session's output directory and plots immediately; replies "Session ended. Call `create_session()` to start a new one." Annotations: title "End Session", `idempotentHint=True`. |
| **all 22 existing tools** | Gained a trailing `session_id` parameter and a resolve-or-error preamble; behaviour otherwise unchanged except as noted below. |
| `query` | Forbidden-procedure filter (`_is_forbidden_query`), 1,000-row cap via streaming `_read_with_count` (now returns `(n, json, truncated)`), 60 s timeout (`tx.run(..., timeout=)`), `asyncio.TimeoutError` and Neo4j "terminated/timeout" errors mapped to a "time limit" message, generic errors reduced to the exception type name, structured `query_rejected` / `query_timeout` / `query_error` log events with scrubbed Cypher. |
| `set_output_directory` | Rejects paths > 4096 chars ("Error: path is too long (max 4096 characters)."), containing NUL/newline, or `..` segments; stores per session; docstring and remote advisory rewritten for the container deployment (the old `python save_plot.py` script wording is gone here and in `get_save_script`/save hints — plots are retrieved via `fetch_plot`/`plot://`). |
| `get_output_directory`, `get_save_script`, `fetch_plot` | Read the calling session's state only; "not in this session's registry" messaging; `fetch_plot` returns the `EmbeddedResource` with the session-scoped URI. |
| `create_volcano_plot`, `create_venn_diagram` | Register plots in the session registry (`_register_plot` now returns `bool`); save hint names `fetch_plot(session_id=…)` / `get_save_script(session_id=…)` / `plot://<sid>/<file>`. These two tools are *not* in the lenient policy's state-bearing set, so under `lenient` they still render without a session but the plot is not retained and the hint says so. |
| `select_assays` | Docstring now leads with "any question about the groups a study contains" (control/experimental groups, comparisons, factors → list mode with just `study_id`), then factor-pair resolution. |
| `get_neo4j_schema`, `get_node_metadata`, `get_relationship_metadata`, `get_study_info`, `find_differentially_*`, `find_common_*`, `clean_mermaid_diagram`, `create_chat_transcript`, `visualize_schema` | `session_id` parameter only; all Neo4j reads now carry the 60 s timeout. |
| `plot://{session_id}/{filename}` resource | Session validated before lookup; raises `ResourceError` (mcp 2.x) so "unknown/expired session — call create_session" and "available plots: …" reach the client. |

**Server instructions (`DEFAULT_INSTRUCTIONS`)** gained a SESSION PROTOCOL block (call `create_session` once, pass `session_id` on every call, re-create on unknown/expired) and a routing line sending "what groups / conditions / control vs experimental groups / what was compared" to `select_assays(study_id=…)`. The `query` docstring's routing table gained the matching entry.

---

## 4. Session isolation (new `src/mcp_genelab/sessions.py`)

- Replaces the module globals `_USER_OUTPUT_DIR` and `_LAST_PLOTS` (removed) with a `SessionStore` of `SessionState` objects (output_dir, plot registry, timestamps, client label/fingerprint). The active session is bound per call via a `contextvars.ContextVar`, so the existing helpers (`_get_user_output_dir`, `_register_plot`, `_lookup_plot`, `_list_registered_plots`, `_resolve_output_paths`, `_write_results_csv`) work unchanged inside tools.
- Session policy `MCP_SESSION_POLICY`: `strict` (default for `streamable-http`/`http`/`sse`; `session_id` is a required schema property, decided at server build time by `_session_id_field()`), `lenient` (only the state-bearing tools `set_output_directory`, `get_output_directory`, `get_save_script`, `fetch_plot`, `end_session` need a session), `implicit` (default for stdio: fixed local session id `local`, which is rejected as an explicit id under `strict`).
- The transport's `Mcp-Session-Id` header is deliberately ignored (in stateless mode it is client-supplied and unvalidated); only ids minted by `create_session` are accepted. Ids are shape-validated before lookup.
- Bounds: idle TTL 3600 s, absolute max age 28800 s, 10,000 live sessions per task (at the cap only sessions idle ≥ 300 s are recycled, otherwise creation is refused so a flood cannot log active users out), 8 plots per session (FIFO), 256 MiB retained plot bytes process-wide (oldest plots of least-recently-used sessions evicted first). Expired/evicted ids are tombstoned so the agent sees "expired" rather than "unknown".
- Thread-safe (single lock); expiry is swept opportunistically on each store access — no background task. Env parsing falls back to defaults on unparsable values; `MAX_LAST_PLOTS` in `server.py` is now an alias of the env-tunable per-session cap (was the literal 8).
- The store is in-memory per task: run one Fargate task, or plug a shared backend (the class interface is small) before scaling out.

---

## 5. Endpoint hardening (server.py)

- `MCP_QUERY_TIMEOUT_SECONDS` (60), `MCP_MAX_QUERY_ROWS` (1000), `MCP_NEO4J_POOL_SIZE` (20, driver `max_connection_pool_size`), `MCP_NEO4J_ACQUISITION_TIMEOUT` (30, driver `connection_acquisition_timeout`), `MCP_LOG_LEVEL` (default INFO; previously hard-coded DEBUG).
- `_WRITE_KEYWORDS_RE` hoisted to module level; new `_FORBIDDEN_PROC_RE` and `_is_forbidden_query()`; `apoc.meta.*` / `apoc.help` intentionally allowed for the schema tools.
- `_require_env()` — remote transports exit with a FATAL message if any Neo4j credential is missing.
- `_scrub_for_log()` — user-supplied Cypher/paths logged only collapsed and truncated (200 chars); the Neo4j URI and query params are no longer logged.
- Neo4j reads keep `default_access_mode=READ_ACCESS` (second, Bolt-level read-only layer).
- `STREAMABLE_HTTP_OPTIONS`: `stateless_http=True`, SSE responses, request body cap `MCP_MAX_REQUEST_BODY_BYTES` (1 MiB), `streamable_http_path` = `MCP_PATH_PREFIX` + `/mcp`.
- Container runs as an unprivileged user and writes nothing to disk in remote mode.

---

## 6. Health routes and usage metrics (new `src/mcp_genelab/metrics.py`)

**HTTP routes** (same app/port as `/mcp`): `GET /healthz` (liveness, no DB; returns service name and version), `GET /readyz` (Neo4j `RETURN 1` with `MCP_READYZ_TIMEOUT_SECONDS`=4 s budget, 503 `degraded` on failure, verdict cached `MCP_READYZ_CACHE_SECONDS`=2 s), `GET /metrics` (only when `MCP_METRICS_ENDPOINT=1`; Prometheus text or JSON).

**Path prefix** (`MCP_PATH_PREFIX`, default empty): when set, *every* route moves under the prefix and nothing is served at the root — `MCP_PATH_PREFIX=/kg` gives `POST /kg/mcp`, `GET /kg/healthz`, `GET /kg/readyz`, `GET /kg/metrics`. New module constants `PATH_PREFIX`, `MCP_PATH`, `HEALTHZ_PATH`, `READYZ_PATH`, `METRICS_PATH` and helper `_normalize_prefix()` (`kg`, `/kg/`, `/kg` → `/kg`; `/`, blank → none). The ALB listener rule path pattern (`/kg/*`), the target-group health-check path (`/kg/readyz`) and the container healthCheck must use the prefixed paths. The JSON usage log and EMF documents are unaffected (they go to stderr, not over HTTP).

**Usage metrics**, no AWS SDK required:
- `MCP_USAGE_LOG` (default on for remote transports, off for stdio): one JSON line per event on **stderr** — `tool_call` (tool, status `ok`/`error`/`exception`, duration_ms, session digest, client, client_fp), `session_created`, `session_rejected` (reason), and `client_initialized` (from the MCP `initialize` handshake: `clientInfo` name/version, negotiated protocol version, User-Agent). Session ids are logged only as a 12-hex SHA-256 digest.
- `client_fp`: salted SHA-256 (16 hex) of viewer IP (first `X-Forwarded-For` hop, falling back to `CloudFront-Viewer-Address`) + User-Agent, salt from `MCP_USAGE_FP_SALT` (random per process if unset) — groups the conversations of one client so "distinct chats per user" is answerable; raw IPs are never logged. Every event also carries `service` (`MCP_SERVICE_NAME`, default `mcp-genelab`, also the EMF `Service` dimension), `error_type` and `ts`.
- `MCP_METRICS_EMF=1`: CloudWatch Embedded Metric Format documents (`ToolCalls`, `ToolErrors`, `ToolLatencyMs` by Service/Tool; `SessionsCreated`; `SessionsRejected` by reason; `ClientInitializations` by client name), namespace `MCP_METRICS_NAMESPACE`.
- In-process counters (per-tool calls/errors/latency, sessions, clients, plots) behind `/metrics`.
- Instrumentation wraps the SDK's tool dispatch (`_install_usage_metrics`; also resets the session ContextVar after each call) and an mcp 2.x `ServerMiddleware` observes `initialize` (`_client_identification_middleware`). A call counts as `error` when the result is `is_error` or its first text block starts with "Error" — so the `query` tool's new rejection/timeout texts (which no longer start with `Error:`) are counted as `ok`, not errors.
- Route details: `/healthz` and `/readyz` accept `GET` and `HEAD`, reply with `Cache-Control: no-store`; `/readyz` bodies are `{"status":"ready","neo4j":"ok"}` or `{"status":"degraded","neo4j":"<ExceptionType>"}` (503) and the ping uses the same read-only pool; `/metrics` returns JSON when `Accept: application/json`, otherwise Prometheus text (`text/plain; version=0.0.4`). Startup now logs the session policy/bounds, metrics switches and the HTTP route table (with the resolved `MCP_PATH_PREFIX`); the Neo4j URI is no longer logged.
- The server binds `0.0.0.0` in the container, so the SDK's DNS-rebinding protection (auto-enabled only for loopback binds) stays off and the public `Host` header forwarded by CloudFront/ALB is accepted.

---

## 7. MCP Python SDK 2.x port

- `from mcp.server.fastmcp import FastMCP` → `from mcp.server.mcpserver import MCPServer, Context`; `MCPServer(name=…, instructions=…, version=__version__, dependencies=…, middleware=[…])` — the server now advertises its version in `serverInfo`.
- Transport options moved from the constructor to `run_streamable_http_async(host, port, **STREAMABLE_HTTP_OPTIONS)` / `run_sse_async(host, port)`; `create_mcp_server(driver, database, instructions)` no longer takes host/port.
- Request headers come from the injected `Context` (`ctx.headers`); the 1.x global request context is gone.
- Types: `types.ContentBlock`, `mime_type=` on `ImageContent`/`BlobResourceContents`, stray `mimeType=` kwargs on `TextContent` removed; resource functions raise `ResourceError`.
- `uv.lock` re-locked: mcp 1.20.0 → 2.2.0 (+ `mcp-types`, `opentelemetry-api`, `httpx2`, `httpcore2`, `httpx2-jsfetch`, `truststore`); `httpx`, `httpx-sse`, `httpcore`, `pydantic-settings`, `python-dotenv`, `certifi` dropped.
- `server.py` file mode changed from executable (`100755`) to regular (`100644`).

---

## 8. Dockerfile

- `FROM --platform=linux/arm64 python:3.12-slim` (ARM64 image; the ECS task definition must declare `cpuArchitecture: ARM64`).
- **Removed** baked-in `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`; `NEO4J_DATABASE` default `neo4j` → `spoke-genelab-v0.3.1`.
- Added non-root user (`mcp`, uid 10001).
- Added defaults: `MCP_QUERY_TIMEOUT_SECONDS=60`, `MCP_MAX_QUERY_ROWS=1000`, `MCP_NEO4J_POOL_SIZE=20`, `MCP_NEO4J_ACQUISITION_TIMEOUT=30`, `MCP_LOG_LEVEL=INFO`, `MCP_SESSION_POLICY=strict`, `MCP_SESSION_IDLE_TTL_SECONDS=3600`, `MCP_SESSION_MAX_AGE_SECONDS=28800`, `MCP_MAX_SESSIONS=10000`, `MCP_SESSION_EVICTION_MIN_IDLE_SECONDS=300`, `MCP_MAX_PLOTS_PER_SESSION=8`, `MCP_MAX_TOTAL_PLOT_BYTES=268435456`, `MCP_USAGE_LOG=1`, `MCP_METRICS_EMF=0`, `MCP_METRICS_ENDPOINT=0`, `MCP_READYZ_TIMEOUT_SECONDS=4`, `MCP_READYZ_CACHE_SECONDS=2`, `MCP_PATH_PREFIX=""` (routes at the root unless overridden in the task definition).
- Added a stdlib-based `HEALTHCHECK` on `/healthz` (`--interval=30s --timeout=5s --start-period=20s --retries=3`; no curl in the image; the command reads `MCP_PATH_PREFIX` at probe time so it follows the prefix, e.g. `/kg/healthz`; ECS ignores it — copy the command into the task definition); user created with `useradd --system --no-create-home`; header comments describe the ALB/Fargate HTTP contract. `MCP_TRANSPORT=streamable-http`, `MCP_HOST=0.0.0.0`, `MCP_PORT=8000`, `EXPOSE 8000` unchanged.

---

## 9. Configuration, packaging, docs

- `config/claude_desktop_config.json`, `claude_desktop_config_dev.json`, `mcp.json`, `mcp_dev.json`: added a `_securityNote` (local placeholders only; hosted deployment takes injected secrets); args reformatted; no values changed.
- `pyproject.toml`: `mcp>=2.2.0,<3`, `starlette>=0.27`, second author added (Amanda Saravia-Butler), description updated; version 0.5.0.
- `docs/deployment.md` **(new, ~500 lines)**: public-endpoint runbook — architecture, secrets/egress, full env reference, query cost controls, WAF, session isolation, ECS/ALB configuration (target group, task definition, service, security groups, certificates), observability and per-conversation metrics with Logs Insights queries, Neo4j hardening, auth per client, pre-launch checklist, known limitations; `MCP_PATH_PREFIX` documented in the env reference, health-check section (prefixed `HealthCheckPath` / container healthCheck), sample task definition, `/metrics` paragraph and checklist.
- `docs/api.md`: 24 tools; new "Sessions (public endpoint)" section; `create_session`/`end_session` entries; `session_id` parameter; `plot://{session_id}/{filename}`.
- `README.md`: session-scoping and remote-deployment feature bullets, health routes in the Docker section, expanded environment-variable table (required-in-remote credentials, session/metrics/query-control variables, `MCP_PATH_PREFIX`), 24-tool reference with a "Sessions and output paths" group, testing paragraph.

---

## 10. Tests (`mcp-genelab-tests/`)

- **New `test_endpoint_hardening.py`** (13 test functions, 34 collected with parametrization): forbidden procedures rejected / read-only + `apoc.meta` allowed, row-cap truncation and header, `tx.run` receives the timeout, Cypher-timeout message, errors don't echo the query, `set_output_directory` bad/good paths, `_require_env`, `_is_forbidden_query`, `_scrub_for_log`.
- **New `test_session_isolation.py`** (37 test functions, 53 collected): session lifecycle, cross-session invisibility of output dir and plots (tools and resource), strict/lenient/implicit policies and error messages, `session_id` required in every schema under strict and optional under implicit, `DEFAULT_INSTRUCTIONS` carries the session protocol, expiry and plot drop, FIFO/byte/session-cap bounds and the capacity refusal, forged `Mcp-Session-Id` header ignored, `/healthz`, `/readyz` (200/503/timeout/cache), `/metrics` off-by-default and content, usage log/EMF shape, client fingerprint, client identification on `initialize`, and `MCP_PATH_PREFIX` (prefix normalisation, root defaults, and a `/kg` run asserting `/kg/mcp`, `/kg/healthz`, `/kg/readyz`, `/kg/metrics` answer while the root paths 404).
- `conftest.py`: package discovery for the `mcp-genelab-tests/` layout and pip-installed fallback; fake driver supports `timeout=` and `async for`; autouse `local_session` fixture; `strict_policy` / `strict_mcp_server`; `_content_of()` for mcp 2.x `CallToolResult`.
- Updated: `test_cypher_invariants.py`, `test_data_tools.py` (new rejection wording), `test_plot_outputs.py` (session-scoped URI, `_content_of`, snake_case `uri_template`/`mime_type`), `test_tools_list.py` (24 tools, snake_case annotation attributes), `test_top_n_widening.py` (`input_schema`), `test_uncovered_tools.py` (session registry, URI).
- `requirements-test.txt`: `mcp>=2.2.0,<3`, `httpx`; `.github/workflows/test.yml`: comment only (matrix 3.10–3.13 unchanged); test READMEs updated.
- Unchanged and still passing: `test_common_tools.py`, `test_query_routing.py`, `test_specialist_docstrings.py`, `pytest.ini`.

---

## 11. Environment variables added in 0.5.0

`MCP_LOG_LEVEL`, `MCP_QUERY_TIMEOUT_SECONDS`, `MCP_MAX_QUERY_ROWS`, `MCP_NEO4J_POOL_SIZE`, `MCP_NEO4J_ACQUISITION_TIMEOUT`, `MCP_MAX_REQUEST_BODY_BYTES`, `MCP_READYZ_TIMEOUT_SECONDS`, `MCP_READYZ_CACHE_SECONDS`, `MCP_SESSION_POLICY`, `MCP_SESSION_IDLE_TTL_SECONDS`, `MCP_SESSION_MAX_AGE_SECONDS`, `MCP_MAX_SESSIONS`, `MCP_SESSION_EVICTION_MIN_IDLE_SECONDS`, `MCP_MAX_PLOTS_PER_SESSION`, `MCP_MAX_TOTAL_PLOT_BYTES`, `MCP_USAGE_LOG`, `MCP_USAGE_FP_SALT`, `MCP_METRICS_EMF`, `MCP_METRICS_NAMESPACE`, `MCP_METRICS_ENDPOINT`, `MCP_SERVICE_NAME`, `MCP_PATH_PREFIX`. Pre-existing and unchanged: `NEO4J_*`, `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `INSTRUCTIONS`.

---

## 12. Files not changed between 0.4.0 and 0.5.0

`LICENSE`, `.gitignore`, `glama.json`, `src/mcp_genelab/__init__.py`, `docs/installation.md`, `docs/development.md`, `docs/build_publish.md`, `docs/examples/**`, `docs/images/**`, `mcp-genelab-tests/pytest.ini`, `test_common_tools.py`, `test_query_routing.py`, `test_specialist_docstrings.py`.


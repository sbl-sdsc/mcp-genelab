# MCP-GeneLab — Public Endpoint Deployment Runbook

This runbook covers deploying `mcp-genelab` as a **public Streamable HTTP endpoint**. It is written for the target architecture:

```
MCP client (Claude Desktop / VS Code+Copilot / etc.)
        │  HTTPS (443)
        ▼
Amazon CloudFront ──► AWS WAF (rate-based rule + managed rule sets)
        │
        ▼
Bedrock AgentCore Gateway   ── inbound auth: CUSTOM_JWT
        │
        ▼   outbound auth: OAuth-M2M / SigV4
Bedrock AgentCore Runtime   ── one isolated Firecracker microVM PER SESSION
        │
        ▼   Bolt (private network), READ_ACCESS
Neo4j (spoke-genelab-v0.3.1)  ── SHARED across all microVMs
```

The two facts that drive every decision below:

1. **Each user session gets its own microVM.** Process-level state in the
   server (module globals like `_USER_OUTPUT_DIR`, the `_LAST_PLOTS` registry)
   is **not shared across users** — there is no cross-tenant leak through those
   globals, because User A and User B are in different VMs with different
   process memory. This is why the server can safely use a module global for
   the per-session output directory.

2. **Neo4j is the one shared resource.** It sits behind all the microVMs.
   Anything that protects Neo4j (connection-pool sizing, query timeouts) has to
   account for `pool_size × concurrent_sessions` load, and per-request rate
   limiting happens at the WAF (which can see identity/IP), not in-process
   (which cannot).

---

## 1. Secrets — credentials are not included in the image

The server **refuses to start** in remote transport if `NEO4J_URI`,
`NEO4J_USERNAME`, or `NEO4J_PASSWORD` are missing (`_require_env()` in
`server.py`). The `Dockerfile` deliberately sets **no** credential defaults.

The credentials are injected at runtime from AWS Secrets Manager. Example ECS/AgentCore task
definition fragment:

```json
"secrets": [
  {"name": "NEO4J_USERNAME", "valueFrom": "arn:aws:secretsmanager:<region>:<acct>:secret:mcp-genelab/neo4j-XXXX:username::"},
  {"name": "NEO4J_PASSWORD", "valueFrom": "arn:aws:secretsmanager:<region>:<acct>:secret:mcp-genelab/neo4j-XXXX:password::"}
],
"environment": [
  {"name": "NEO4J_URI", "value": "bolt://<private-neo4j-host>:7687"},
  {"name": "NEO4J_DATABASE", "value": "spoke-genelab-v0.3.1"},
  {"name": "MCP_TRANSPORT", "value": "streamable-http"}
]
```

Grant the task **execution role** `secretsmanager:GetSecretValue` on that ARN
only. Use a **read-only Neo4j user** for this service (see §6).

---

## 2. Environment variable reference

| Variable | Required (remote) | Default | Purpose |
|---|---|---|---|
| `NEO4J_URI` | **yes** | — (fail-fast) | Bolt URI of the shared Neo4j |
| `NEO4J_USERNAME` | **yes** | — (fail-fast) | Read-only service user |
| `NEO4J_PASSWORD` | **yes** | — (fail-fast) | From Secrets Manager |
| `NEO4J_DATABASE` | no | `spoke-genelab-v0.3.1` | KG database name |
| `MCP_TRANSPORT` | no | `stdio` | Set to `streamable-http` for the endpoint |
| `MCP_HOST` | no | `127.0.0.1` | Bind host (`0.0.0.0` in container) |
| `MCP_PORT` | no | `8000` | Bind port |
| `MCP_QUERY_TIMEOUT_SECONDS` | no | `60` | Per-query cancel deadline |
| `MCP_MAX_QUERY_ROWS` | no | `1000` | Row cap for the `query` tool |
| `MCP_NEO4J_POOL_SIZE` | no | `20` | Per-microVM connection pool (keep modest) |
| `MCP_NEO4J_ACQUISITION_TIMEOUT` | no | `30` | Fail-fast on pool saturation |
| `MCP_LOG_LEVEL` | no | `INFO` | `DEBUG` logs full queries (avoid in prod) |
| `INSTRUCTIONS` | no | built-in policy | Tool-selection policy; leave unset |

In local **stdio** mode the Neo4j credentials fall back to
`bolt://localhost:7687` / `neo4j` / `neo4jdemo` for developer convenience —
the fail-fast policy applies only to remote transports.

---

## 3. Query cost controls (protect the shared Neo4j)

microVM isolation does **not** protect the shared database from a single
expensive query. Three controls do:

- **Per-query timeout** (`MCP_QUERY_TIMEOUT_SECONDS`, default 60s). Passed to
  `tx.run(..., timeout=)` so Neo4j cancels a runaway traversal server-side and
  returns the connection to the pool.
- **Row cap** (`MCP_MAX_QUERY_ROWS`, default 1000) on the general-purpose
  `query` tool. The specialist tools already bound output via `top_n`; this
  protects the raw-Cypher fallback. Truncation appends a notice telling the
  model to add a `LIMIT`.
- **Connection pool sizing** (`MCP_NEO4J_POOL_SIZE`, default 20, plus a 30s
  acquisition timeout). Because every microVM has its own pool against one
  Neo4j, reason about `pool_size × max_concurrent_sessions` vs. Neo4j's total
  connection capacity, and keep the per-VM pool **modest** — 10–20 is usually
  better than the driver default of 100.

---

## 4. WAF — rate limiting and managed rules

Per-user request-rate limiting is enforced at the WAF, which (unlike the
in-process server) can see the client IP and JWT identity. Attach a WebACL to
the CloudFront distribution:

```json
{
  "Name": "mcp-genelab-rate-limit",
  "Priority": 10,
  "Statement": {
    "RateBasedStatement": {
      "Limit": 300,
      "EvaluationWindowSec": 300,
      "AggregateKeyType": "IP",
      "ScopeDownStatement": {
        "ByteMatchStatement": {
          "SearchString": "/mcp",
          "FieldToMatch": {"UriPath": {}},
          "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
          "PositionalConstraint": "STARTS_WITH"
        }
      }
    }
  },
  "Action": {"Block": {"CustomResponse": {"ResponseCode": 429}}},
  "VisibilityConfig": {"SampledRequestsEnabled": true, "CloudWatchMetricsEnabled": true, "MetricName": "mcpRateLimit"}
}
```

Notes:
- The `Limit`/`EvaluationWindowSec` values (300 requests / 300 seconds per IP)
  are a **starting point**. One tester working an example in one MCP session 
  can issue many tool calls per minute. The rule will be deployed in **Count**
  mode for the beta test, and `SampledRequests` / CloudWatch will be monitored. 
  Based on the results, the number will be set and then switched to **Block**.
- `AggregateKeyType: IP` is coarse (shared corporate NAT = shared budget).
  Because the Gateway does **CUSTOM_JWT** auth, prefer keying is on the
  authenticated identity where possible (custom-header aggregation) with IP as
  a secondary layer.
- Added `AWSManagedRulesCommonRuleSet` and
  `AWSManagedRulesAmazonIpReputationList` at lower priority.
- Capped request body size at CloudFront/Gateway so a giant Cypher string can't be
  posted.

---

## 5. AgentCore MCP contract & health

This detail was verified against the AWS documentation
(`docs.aws.amazon.com/bedrock-agentcore/.../runtime-mcp-protocol-contract.html`
and the "Deploy MCP servers in AgentCore Runtime" guide).

**mcp-genelab is an MCP server, so it uses the MCP protocol contract — NOT the
HTTP/"agent" contract.** The differences are shown below:

| | MCP protocol contract (this server) | HTTP / "agent" contract (not this server) |
|---|---|---|
| Port | **8000** | 8080 |
| Path | **`/mcp` (POST)** | `/invocations` (POST) |
| Health path | **none — no `/ping`** | `/ping` (GET) |
| Transport | streamable-http, `stateless_http=True` | JSON / SSE |
| Platform | ARM64, host `0.0.0.0` | ARM64, host `0.0.0.0` |

Notes:

- **There is no `/ping` health endpoint to implement.** The MCP contract lists
  exactly one path requirement, `/mcp` (POST). AgentCore infers health from the
  `/mcp` endpoint.
- **The image MUST be ARM64.** The runtime rejects amd64 images. The Dockerfile
  pins `FROM --platform=linux/arm64 …`; if building on x86_64 without
  emulation use `docker buildx build --platform linux/arm64`.
- **`stateless_http=True` is a contract requirement**.
  The server runs stateless streamable-http by default. It must accept the
  platform-injected `Mcp-Session-Id` header and not reject it (FastMCP in
  stateless mode does this; verified against the pinned `mcp`/fastmcp version).
  AgentCore uses `Mcp-Session-Id` for microVM stickiness — the client captures
  the id returned in the response and echoes it on subsequent requests so they
  route back to the same microVM.
- Stateful mode (`stateless_http=False`) is *supported* by AgentCore and gives
  the strongest per-session microVM isolation (dedicated microVM per session,
  up to 8h / 15min idle). This is not currently implemented but can be used if
  elicitation/sampling is needed; note that it will
  change the session-lifetime and quota semantics.

**Optional — non-AgentCore staging behind an ALB.** If (and only if) this image
is run behind an ALB target group in a non-AgentCore environment, that
path *does* need an HTTP GET health route, which the MCP `/mcp` POST endpoint
cannot serve. Add one to the Starlette app FastMCP exposes:

```python
app = mcp.streamable_http_app()   # verify accessor for your fastmcp version

async def healthz(request):
    return JSONResponse({"status": "ok"})

async def readyz(request):
    try:
        async with driver.session(database=db, default_access_mode=READ_ACCESS) as s:
            await asyncio.wait_for(s.run("RETURN 1"), timeout=5)
        return JSONResponse({"status": "ready"})
    except Exception:
        return JSONResponse({"status": "degraded"}, status_code=503)

app.add_route("/healthz", healthz, methods=["GET"])
app.add_route("/readyz", readyz, methods=["GET"])
```

This is **not** needed for AgentCore itself — only for an ALB-fronted staging
deployment. Split liveness (no DB) from readiness (checks Neo4j) so a brief DB
blip doesn't trigger container restarts.

---

## 6. Neo4j hardening

- On Neo4j Community Edition, rely on the server's `READ_ACCESS` sessions + the
  application-layer forbidden-procedure filter (below) +
  `dbms.security.procedures.allowlist`.
- The server already enforces read-only two ways: every session opens with
  `default_access_mode=READ_ACCESS`, and the `query` tool rejects writes AND
  read-only-but-dangerous procedures (`LOAD CSV`, `apoc.load.*`,
  `apoc.export.*`, `dbms.*`) via `_is_forbidden_query()`.
- **APOC** must be installed for the schema tools (`apoc.meta.*`). Without it,
  those tools return a friendly error; the analysis tools still work.
- Neo4j is hosted on a private EC2 instance; Bolt is only allowed from 
  the runtime's security group.

---

## 7. Observability

- Logs go to **stdout** (JSON-friendly). Set `MCP_LOG_LEVEL=INFO` in prod —
  `DEBUG` logs full Cypher, which is only used for debugging. User queries 
  are logged only in **scrubbed, truncated** form (`_scrub_for_log`) at INFO.
- Useful CloudWatch metric filters on the logs: `query_rejected`,
  `query_timeout`, `query_error` counts; alarm on a spike.
- WAF `BlockedRequests` and Neo4j connection-acquisition failures are monitored.

---

## 8. Pre-launch checklist

- [ ] Image is **ARM64** (`docker inspect <img> --format '{{.Architecture}}'` → `arm64`).
- [ ] MCP contract satisfied: port **8000**, host `0.0.0.0`, `/mcp` endpoint reachable, `stateless_http=True`. No `/ping` route is expected by AgentCore.
- [ ] Container accepts a platform-injected `Mcp-Session-Id` header without rejecting it.
- [ ] Image contains **no** `NEO4J_PASSWORD` (`docker inspect ... | grep -i password` → empty).
- [ ] Container **refuses to start** without creds in remote mode.
- [ ] Neo4j service user is **read-only**; APOC installed.
- [ ] WAF rate rule attached (Count → tuned → Block); managed rule sets on.
- [ ] Query timeout + row cap verified against the live KG.
- [ ] Pool size sized for `pool × concurrent_sessions` vs Neo4j capacity.
- [ ] `pytest` green (146 tests) including endpoint-hardening suite.

---

## 9. Known limitations

- The MCP endpoint itself is unauthenticated at the app layer; auth is at the
  Gateway (CUSTOM_JWT). Do not rely on the app for identity.
- The module-global output-directory / plot-registry design is safe **only
  because** of AgentCore's per-session microVM isolation. If this service is
  ever redeployed as a single shared process serving multiple users (e.g. a
  plain ECS/Fargate task without per-session isolation), that state becomes
  cross-tenant and would be refactored to per-session scoping (e.g. `ContextVar`
  keyed on the MCP session id) before launch. This constraint is noted in
  `server.py` near `_USER_OUTPUT_DIR`.

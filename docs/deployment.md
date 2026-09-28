# MCP-GeneLab — Public Endpoint Deployment Runbook (ECS Fargate)

This runbook covers deploying `mcp-genelab` as a **public Streamable HTTP endpoint** on the target architecture:

```
MCP client (ChatGPT Developer Mode / Claude / Cursor / Claude Code / curl …)
        │  HTTPS (443)
        ▼
Amazon CloudFront ──► AWS WAF web ACL (rate-based rule + managed rule sets)
        │                                   ACM cert: us-east-1
        ▼
Application Load Balancer (HTTPS, public subnets)   ACM cert: deployment region
        │   target group → GET /readyz health check
        ▼
ECS Fargate service (ARM64 task, private subnets)   ── ONE shared process
        │   Bolt 7687 (private), READ_ACCESS               serving ALL users
        ▼
Neo4j Community (spoke-genelab-v0.3.1) on EC2 (private) ── SHARED by all tasks
```

WAF attaches to CloudFront (or to the ALB) — never to bare compute. Nothing in
this topology negotiates MCP on the client's behalf: the container terminates
Streamable HTTP itself and negotiates the protocol version directly with each
client (the pinned SDK, `mcp>=2.2,<3`, advertises `2026-07-28` and negotiates
down to whatever the client supports, e.g. `2025-11-25` or `2025-06-18`).

The two facts that drive every decision below:

1. **There is no per-user process isolation.** One long-lived process serves
   every user, and the ALB may spread one user's requests across tasks. Any
   per-user state therefore has to be keyed on an explicit, application-level
   **session id** (§5). The server keeps **no** per-user module globals.

2. **Neo4j is the one shared resource.** Every task has one Bolt connection
   pool; all pools draw on one Neo4j. Reason about `pool_size × number_of_tasks`
   against Neo4j's connection ceiling (§3). Per-client request-rate limiting
   happens at the WAF (which can see the client IP), not in-process.

---

## 1. Secrets — credentials are not included in the image

The server **refuses to start** in remote transport if `NEO4J_URI`,
`NEO4J_USERNAME`, or `NEO4J_PASSWORD` are missing (`_require_env()` in
`server.py`). The `Dockerfile` deliberately sets **no** credential defaults.

The credentials are injected at runtime from AWS Secrets Manager via the ECS task
definition:

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
only. Use a **read-only Neo4j user** for this service (see §8). Private-subnet
tasks reach Secrets Manager and ECR through a NAT gateway or VPC interface
endpoints (`com.amazonaws.<region>.secretsmanager`, `ecr.api`, `ecr.dkr`, plus
an S3 gateway endpoint for image layers and `logs` for CloudWatch); endpoints
avoid the NAT gateway's hourly cost when that is the only egress the task needs.

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
| `MCP_NEO4J_POOL_SIZE` | no | `20` | Per-**task** connection pool |
| `MCP_NEO4J_ACQUISITION_TIMEOUT` | no | `30` | Fail-fast on pool saturation |
| `MCP_READYZ_TIMEOUT_SECONDS` | no | `4` | Budget for the `/readyz` Neo4j ping (keep < ALB health-check timeout) |
| `MCP_MAX_REQUEST_BODY_BYTES` | no | `1048576` | Maximum size of one `POST /mcp` body (1 MiB); second cap behind the WAF body-size rule |
| `MCP_PATH_PREFIX` | no | *(empty)* | Public path prefix when the ALB/CloudFront publish the service under one (they cannot rewrite paths). `/kg` → `POST /kg/mcp`, `GET /kg/healthz`, `/kg/readyz`, `/kg/metrics`. Must match the listener-rule path pattern (`/kg/*`) and the health-check paths below |
| `MCP_SESSION_POLICY` | no | `strict` (remote) / `implicit` (stdio) | `strict`: every tool except `create_session` requires a valid `session_id`. `lenient`: only state-bearing tools require it. `implicit`: fixed local session (stdio only). |
| `MCP_SESSION_IDLE_TTL_SECONDS` | no | `3600` | Session dropped after this long without a call |
| `MCP_SESSION_MAX_AGE_SECONDS` | no | `28800` | Absolute session lifetime (8 h) |
| `MCP_MAX_SESSIONS` | no | `10000` | Live-session cap per task. At the cap only sessions idle ≥ `MCP_SESSION_EVICTION_MIN_IDLE_SECONDS` are recycled (LRU first); otherwise `create_session` is refused — a flood cannot log active users out |
| `MCP_SESSION_EVICTION_MIN_IDLE_SECONDS` | no | `300` | Minimum idle time before a session may be recycled to make room at the `MCP_MAX_SESSIONS` cap |
| `MCP_READYZ_CACHE_SECONDS` | no | `2` | Reuse the last `/readyz` verdict for this long (probe bursts can't amplify into Neo4j load) |
| `MCP_MAX_PLOTS_PER_SESSION` | no | `8` | Plots retained per session (FIFO) |
| `MCP_MAX_TOTAL_PLOT_BYTES` | no | `268435456` | Process-wide cap on retained PNG bytes (256 MiB) |
| `MCP_USAGE_LOG` | no | `1` (remote) / `0` (stdio) | One JSON line per tool call on stderr (never stdout — in stdio mode that is the JSON-RPC wire) |
| `MCP_USAGE_FP_SALT` | recommended | random per process | Salt for the `client_fp` usage-log field (viewer IP + User-Agent → digest). Inject from Secrets Manager so fingerprints are comparable across task restarts |
| `MCP_METRICS_EMF` | no | `0` | Also emit CloudWatch Embedded Metric Format documents |
| `MCP_METRICS_NAMESPACE` | no | `mcp-genelab` | CloudWatch namespace for EMF metrics |
| `MCP_SERVICE_NAME` | no | `mcp-genelab` | `service` field on every usage-log event and the `Service` EMF dimension |
| `MCP_METRICS_ENDPOINT` | no | `0` | Expose `GET /metrics` (Prometheus text / JSON) |
| `MCP_LOG_LEVEL` | no | `INFO` | `DEBUG` logs full queries (avoid in prod) |
| `INSTRUCTIONS` | no | built-in policy | Session protocol + tool-selection policy; leave unset |

In local **stdio** mode the Neo4j credentials fall back to
`bolt://localhost:7687` / `neo4j` / `neo4jdemo` for developer convenience —
the fail-fast policy applies only to remote transports.

---

## 3. Query cost controls (protect the shared Neo4j)

A single expensive query can hurt every user on every task. Three controls:

- **Per-query timeout** (`MCP_QUERY_TIMEOUT_SECONDS`, default 60s). Passed to
  `tx.run(..., timeout=)` so Neo4j cancels a runaway traversal server-side and
  returns the connection to the pool.
- **Row cap** (`MCP_MAX_QUERY_ROWS`, default 1000) on the general-purpose
  `query` tool. The specialist tools already bound output via `top_n`; this
  protects the raw-Cypher fallback. Truncation appends a notice telling the
  model to add a `LIMIT`.
- **Connection pool sizing** (`MCP_NEO4J_POOL_SIZE`, default 20, plus a 30s
  acquisition timeout). Each Fargate task has ONE pool shared by all of its
  sessions, so the load on Neo4j is `pool_size × desiredCount` (plus the
  `/readyz` pings, which use the same pool). Community Edition has no
  server-side per-user throttling; keep the per-task pool at 10–20 and make sure
  `pool_size × tasks` stays under the ceiling you set on the EC2 instance
  (`server.bolt.thread_pool_max_size` and the JVM heap are the practical
  limits). A saturated pool fails fast with a clear error instead of hanging.

---

## 4. WAF — rate limiting and managed rules

Per-client request-rate limiting is enforced at the WAF, which (unlike the
in-process server) can see the client IP. Attach a web ACL to the CloudFront
distribution (scope `CLOUDFRONT`, created in us-east-1):

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
  can issue many tool calls per minute. Deploy the rule in **Count** mode for
  the beta, watch `SampledRequests` / CloudWatch, then tune and switch to
  **Block**.
- `AggregateKeyType: IP` is coarse (shared corporate NAT = shared budget).
  There is no authenticated identity on the no-auth route, so IP is the key;
  if an OAuth route is added later (§9), a second rule keyed on a custom
  header can sit alongside it.
- Add `AWSManagedRulesCommonRuleSet` and
  `AWSManagedRulesAmazonIpReputationList` at lower priority.
- Cap the request body size (a `SizeConstraintStatement` on `Body`, e.g.
  64 KB) so a giant Cypher string can't be posted. The server's own transport
  enforces a second cap, `MCP_MAX_REQUEST_BODY_BYTES` (default 1 MiB, passed
  to the SDK as `max_request_body_size` in `STREAMABLE_HTTP_OPTIONS`); a POST
  larger than that is rejected by the container even if it passes the WAF.
- Restrict CloudFront → ALB with a custom origin header the ALB listener rule
  requires (or the CloudFront prefix-list on the ALB security group), so the
  WAF cannot be bypassed by hitting the ALB directly.

---

## 5. Session isolation (application level)

### Why it is needed

On Fargate **one process serves everyone**. Two things make transport-level session ids unusable for scoping state here
(verified empirically against the MCP Python SDK: 1.20, 1.29 and the pinned 2.x line):

- In `stateless_http=True` mode (what the server runs) the SDK issues **no**
  `Mcp-Session-Id` header — and does not validate one either: a client-supplied
  header passes through unvalidated, so it is attacker-chosen and the server
  deliberately ignores it (two clients sending the same value must never share
  state; `test_forged_mcp_session_id_header_is_ignored` guards this).
- In stateful mode the SDK binds each session to the process that created it,
  and ALB stickiness is **cookie-based only** (it cannot key on a header), so a
  request landing on another task fails with "session not found".

### How it works

Session identity is therefore an explicit, application-level contract enforced
by `MCP_SESSION_POLICY=strict` (the Docker default):

1. The agent calls **`create_session`** once. The server mints an opaque
   43-character URL-safe token (`secrets.token_urlsafe(32)`) and returns
   `session_id: <token>`.
2. The agent passes that value as the **`session_id` argument on every other
   tool call** (all 23 remaining tools expose the parameter). The server
   validates the shape, looks it up in `SESSION_STORE`, binds the matching
   `SessionState` to a `contextvars.ContextVar` for the duration of the call,
   and touches its last-seen time. Output directory and the plot registry are
   read/written through that ContextVar — there is no other place they live.
3. Plot resources are addressed as **`plot://{session_id}/{filename}`**, so a
   `resources/read` from one session can never resolve another session's plot.
   `fetch_plot`, `get_save_script` and every save hint emit the scoped URI.
4. Under `strict` the tool schemas mark `session_id` as **required**, so
   LLM clients supply it proactively; an empty, malformed, unknown or expired
   id returns an `Error (<reason> session): …` block that tells the agent to
   call `create_session` again. Recently expired/evicted ids are remembered in a
   bounded tombstone list so the agent sees *expired* (regenerate plots) rather
   than *unknown*.
5. **`end_session`** discards a session immediately (optional; sessions also
   expire on their own).

The server's `DEFAULT_INSTRUCTIONS` carries a `SESSION PROTOCOL` block so
clients that surface server instructions do this without prompting.

### Bounds

Everything is bounded so one task can serve many sessions predictably:
idle TTL (1 h), absolute max age (8 h),
`MCP_MAX_SESSIONS` live sessions per task (at the cap, only idle sessions are
recycled; otherwise new sessions are refused rather than evicting active
users), 8 plots per session
(FIFO), and a process-wide cap on retained PNG bytes (256 MiB; oldest plots of
the least-recently-used sessions go first). Expired sessions are swept
opportunistically on every store access — no background task.

### Scaling and `desiredCount`

The session store is **in-memory per task**. That is exactly right for the
launch configuration — **`desiredCount = 1`**, scale vertically (2 vCPU / 4–8 GB
is plenty for this workload; the per-task pool is the real limiter). With
several tasks, a session is only valid on the task that created it: the agent
gets a clear *unknown session* error, calls `create_session` again and
continues — nothing leaks, but plots would have to be regenerated. To scale
horizontally without that, back `SessionStore` with a shared store (Redis /
ElastiCache Serverless is the natural fit; the class interface is small:
`create / get / get_or_create_fixed / end / after_mutation / stats`). ALB
sticky sessions are **not** a fix: they need the client to return a cookie,
which MCP clients do not reliably do.

### What stays where

| State | Fargate Deployment |
|---|---|
| Output directory | `SessionState.output_dir` |
| Plot registry | `SessionState.plots` (per session) |
| Plot resource URI | `plot://{session_id}/{filename}` |
| Session identity | `session_id` tool argument (app) |
| Session lifetime | 8 h / 60 min idle (`MCP_SESSION_*`) |

---

## 6. ECS / ALB configuration

### Health checks

The MCP endpoint is POST-only and cannot answer an ALB health check, which is
always an **HTTP GET** (defaults: path `/`, interval 30 s, timeout 5 s, healthy
threshold 5, unhealthy threshold 2, success codes `200`). The server adds two
GET routes on the same port:

| Route | Checks | Use it for |
|---|---|---|
| `GET /healthz` | process is serving requests; **no** DB call | ECS container `healthCheck` (liveness) |
| `GET /readyz` | Neo4j `RETURN 1` within `MCP_READYZ_TIMEOUT_SECONDS` (4 s); `503 {"status":"degraded"}` on failure | ALB target-group health check (readiness) |

With `MCP_PATH_PREFIX` set, every route moves under the prefix — `/kg/healthz`,
`/kg/readyz` (and `/kg/mcp`, `/kg/metrics`) for `MCP_PATH_PREFIX=/kg` — so the
target-group `HealthCheckPath`, the container `healthCheck` command and the
listener rule's path pattern (`/kg/*`) must all use the prefixed paths.

Splitting the two matters: a Neo4j blip pulls the task from ALB rotation
(readiness) without ECS killing and restarting a perfectly healthy container
(liveness).

Target group:

```json
{
  "Protocol": "HTTP", "Port": 8000, "TargetType": "ip",
  "HealthCheckProtocol": "HTTP", "HealthCheckPath": "/readyz",
  "HealthCheckIntervalSeconds": 30, "HealthCheckTimeoutSeconds": 5,
  "HealthyThresholdCount": 2, "UnhealthyThresholdCount": 2,
  "Matcher": {"HttpCode": "200"}
}
```

(`"HealthCheckPath": "/kg/readyz"` when `MCP_PATH_PREFIX=/kg`.)

Set `deregistration_delay.timeout_seconds` low (e.g. 30) — responses are
short-lived; long-running Cypher is already capped at 60 s.

### Task definition (essentials)

```json
{
  "family": "mcp-genelab",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024", "memory": "4096",
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::<acct>:role/mcp-genelab-exec",
  "containerDefinitions": [{
    "name": "mcp-genelab",
    "image": "<acct>.dkr.ecr.<region>.amazonaws.com/mcp-genelab:<tag>",
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [
      {"name": "MCP_TRANSPORT", "value": "streamable-http"},
      {"name": "MCP_HOST", "value": "0.0.0.0"},
      {"name": "MCP_PORT", "value": "8000"},
      {"name": "MCP_SESSION_POLICY", "value": "strict"},
      {"name": "MCP_METRICS_EMF", "value": "1"},
      {"name": "NEO4J_URI", "value": "bolt://<private-neo4j-host>:7687"},
      {"name": "NEO4J_DATABASE", "value": "neo4j"},
      {"name": "MCP_PATH_PREFIX", "value": "/kg"}
    ],
    "secrets": [ "…see §1…" ],
    "healthCheck": {
      "command": ["CMD-SHELL", "python -c \"import os,urllib.request,sys; p='/'+os.environ.get('MCP_PATH_PREFIX','').strip('/'); p='' if p=='/' else p; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000'+p+'/healthz', timeout=3).status == 200 else 1)\""],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 20
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {"awslogs-group": "/ecs/mcp-genelab", "awslogs-region": "<region>", "awslogs-stream-prefix": "mcp"}
    }
  }]
}
```

The image is ARM64 (`FROM --platform=linux/arm64`), so `runtimePlatform`
**must** say `ARM64` (Fargate platform version 1.4.0 or later; Linux only).
The container health check uses the Python stdlib so the image needs no
`curl`. Container `healthCheck` defaults per the ECS docs: interval 30 s,
timeout 5 s, retries 3; `startPeriod` gives the interpreter + matplotlib import
time to come up before failures count.

### Service

- `desiredCount: 1` at launch (see §5 *Scaling*); `platformVersion: LATEST`
  (≥ 1.4.0 for ARM64); private subnets, `assignPublicIp: DISABLED`.
- Deployment circuit breaker on, with rollback.
- Task security group: **inbound** 8000 only from the ALB security group;
  **outbound** 7687 only to the Neo4j EC2 security group, plus 443 to the VPC
  endpoints / NAT for ECR, Secrets Manager and CloudWatch Logs.
- Neo4j EC2 security group: inbound 7687 only from the task security group.
  Same VPC, private subnets; otherwise VPC peering.

### Certificates and routing

- CloudFront's ACM certificate must live in **us-east-1**; the ALB's in the
  deployment region; both cover the same custom domain.
- The server binds to `0.0.0.0`, so the MCP SDK's DNS-rebinding protection
  (auto-enabled by newer SDK versions only for loopback binds) stays off and
  the public `Host` header forwarded by CloudFront/ALB is accepted.
- CloudFront origin request policy must **forward all headers** the MCP
  transport needs (`Accept`, `Content-Type`, `MCP-Protocol-Version`,
  `Authorization` if an OAuth route is added) and use a cache policy of
  `CachingDisabled` for `/mcp*` (or `/<prefix>/mcp*`); `/healthz` and `/readyz` should be reachable
  only from the ALB/VPC (listener rule: allow when the source is the health
  checker, else 403) so the public cannot probe them; `/readyz` additionally
  caches its verdict for `MCP_READYZ_CACHE_SECONDS` so a burst costs at most
  one Neo4j query per window.

---

## 7. Observability and usage metrics

Behind an ALB the only component that sees tool calls is the server, so it records them itself
(`mcp_genelab/metrics.py`), without any AWS SDK in the image or extra IAM.

**Usage log (default on for remote transports).** One JSON line per tool call on stderr (stdout is reserved for the stdio JSON-RPC wire), shipped by the
`awslogs` driver to CloudWatch Logs:

```json
{"event":"tool_call","service":"mcp-genelab","tool":"create_volcano_plot","session":"3f1a9c0e7b2d","status":"ok","error_type":null,"duration_ms":412.7,"client":"python-httpx/0.28.1","ts":"2026-09-10T16:59:03.580+00:00"}
```

`status` is `ok`, `error` (the tool returned an `Error…` block) or `exception`;
`session` is a 12-hex-char SHA-256 digest — the raw session id is a bearer
secret and is **never** logged. `session_created` (with the same `session`
digest) and `session_rejected` (`reason` = missing/malformed/unknown/expired)
events are emitted too.

**Conversation = session.** The agent calls `create_session` once per chat and
passes the id on every call, so every `tool_call` of one conversation shares
one `session` digest — that is the unit for per-conversation metrics (calls,
errors, latency, which tools). Caveat: a chat idle for more than
`MCP_SESSION_IDLE_TTL_SECONDS` (or older than 8 h) gets a *new* session when it
resumes, so a very long conversation can span two digests.

**Same client across conversations.** The no-auth route has no identity, so the
server adds `client_fp`: a salted SHA-256 (16 hex chars) of the viewer IP (first
`X-Forwarded-For` hop as forwarded by CloudFront/ALB) + User-Agent. All
conversations from one client share it, so *distinct chats per user* is
`count() by client_fp` over `session_created`. It is a heuristic (a shared NAT
with the same client collapses to one fingerprint; one user on two clients
yields two) and not an identity or auth signal; the raw IP is never logged. Set
`MCP_USAGE_FP_SALT` from Secrets Manager or fingerprints only compare within a
task's lifetime. If per-user identity is ever required, the OAuth route (§9)
provides a real subject.

Logs Insights examples:

```
fields @timestamp, tool, status, duration_ms | filter event = "tool_call" | stats count() as calls, avg(duration_ms), pct(duration_ms, 95) by tool
filter event = "tool_call" | stats count() as calls, count_distinct(tool) as tools, sum(status != "ok") as errors by session            # per conversation
filter event = "session_created" | stats count() as conversations by client_fp | sort conversations desc                                  # chats per client
filter event = "session_created" | stats count_distinct(client_fp) as unique_clients, count() as conversations by bin(1d)
filter event = "session_rejected" | stats count() by reason
```

**CloudWatch metrics via EMF (`MCP_METRICS_EMF=1`, recommended in prod).** The
same events are also written as CloudWatch Embedded Metric Format documents,
which CloudWatch Logs converts into metrics automatically: `ToolCalls`,
`ToolErrors`, `ToolLatencyMs` (dimensions `Service` and `Service, Tool`),
`SessionsCreated`, `SessionsRejected` (`Service, Reason`), namespace
`MCP_METRICS_NAMESPACE`. Alarm on `ToolErrors`/`ToolCalls` ratio and p95
`ToolLatencyMs`; graph `SessionsCreated` per day for adoption.

**Pull endpoint (`MCP_METRICS_ENDPOINT=1`, off by default).** `GET /metrics`
(`GET /<prefix>/metrics` with `MCP_PATH_PREFIX`, e.g. `/kg/metrics`) returns
Prometheus text (or JSON with `Accept: application/json`) of in-process
counters plus session-store gauges (`live_sessions`, `retained_plot_bytes`, …).
For a CloudWatch agent / ADOT sidecar scraping `localhost:8000`; if enabled,
block `/metrics` at the ALB listener so it is not public. The push side —
the JSON usage log on stderr and the EMF documents — is written to CloudWatch
Logs by the `awslogs` driver and does not depend on any HTTP path, so the
Logs Insights queries above are unaffected by `MCP_PATH_PREFIX`.

**Application log.** Human-readable log on stderr. `MCP_LOG_LEVEL=INFO` in
prod — `DEBUG` logs full Cypher. User queries are logged only in scrubbed,
truncated form (`_scrub_for_log`). Useful metric filters: `query_rejected`,
`query_timeout`, `readyz: Neo4j check failed`. Also watch WAF `BlockedRequests`,
ALB `HTTPCode_Target_5XX_Count` / `UnHealthyHostCount`, and ECS CPU/memory.

---

## 8. Neo4j hardening

- On Neo4j Community Edition, rely on the server's `READ_ACCESS` sessions + the
  application-layer forbidden-procedure filter +
  `dbms.security.procedures.allowlist`.
- The server enforces read-only two ways: every session opens with
  `default_access_mode=READ_ACCESS`, and the `query` tool rejects writes AND
  read-only-but-dangerous procedures (`LOAD CSV`, `apoc.load.*`,
  `apoc.export.*`, `dbms.*`) via `_is_forbidden_query()`.
- **APOC** must be installed for the schema tools (`apoc.meta.*`). Without it,
  those tools return a friendly error; the analysis tools still work.
- Neo4j is hosted on a private EC2 instance; Bolt is only allowed from the
  Fargate task security group.
- Size the Bolt thread pool / heap for `pool_size × tasks` concurrent
  connections (§3).

---

## 9. Auth per client (no-auth route vs. OAuth route)

The keyless goal is a *client-side* constraint, not a host choice:

- **ChatGPT (Developer Mode)** and dev tools (Claude Code, Cursor, Cline, curl)
  accept a bare URL → the no-auth, WAF-protected route works for them.
- **Claude.ai / Claude Desktop custom connectors** require OAuth with Dynamic
  Client Registration and have no "none" option → they need a separate OAuth
  route in front of the same tasks (Auth0/Okta-style DCR-capable IdP, or
  pre-registered clients via the AWS "Guidance for Deploying MCP Servers on
  AWS" Cognito pattern).

Both routes can terminate on the same ALB (two listener rules / host names) and
the same Fargate service; the app is auth-agnostic. The session id (§5) is
**not** an auth mechanism — it scopes state, nothing more.

---

## 10. Pre-launch checklist

- [ ] Image is **ARM64** (`docker inspect <img> --format '{{.Architecture}}'` → `arm64`) and the task definition says `cpuArchitecture: ARM64`.
- [ ] Container listens on `0.0.0.0:8000`; `POST /mcp`, `GET /healthz`, `GET /readyz` reachable from the ALB subnet.
- [ ] If the public URL has a prefix (`https://host/kg/mcp`): `MCP_PATH_PREFIX=/kg` in the task definition; listener rule path pattern `/kg/*`; target-group health check `/kg/readyz`; container healthCheck resolves `/kg/healthz`.
- [ ] ALB target group health check = `GET /readyz` (or `/<prefix>/readyz`), matcher `200`; ECS container healthCheck = `/healthz` (or `/<prefix>/healthz`).
- [ ] `MCP_SESSION_POLICY=strict`; a tool call without `session_id` returns `Error (missing session)`; two sessions cannot see each other's output directory or plots (`test_session_isolation.py` green).
- [ ] Image contains **no** `NEO4J_PASSWORD` (`docker inspect ... | grep -i password` → empty); container **refuses to start** without creds in remote mode.
- [ ] Neo4j service user is **read-only**; APOC installed; Bolt reachable only from the task SG.
- [ ] Pool sized: `MCP_NEO4J_POOL_SIZE × desiredCount` < Neo4j connection ceiling.
- [ ] WAF rate rule attached to CloudFront (Count → tuned → Block); managed rule sets on; body-size cap; ALB not reachable except via CloudFront.
- [ ] Certificates: CloudFront (us-east-1) and ALB (region) for the same domain.
- [ ] `MCP_METRICS_EMF=1`; `ToolCalls` metric visible in CloudWatch after the first call; Logs Insights queries in §7 return rows.
- [ ] `pytest` green (193 tests) including `test_endpoint_hardening.py` and `test_session_isolation.py`.

---

## 11. Known limitations

- The no-auth route is unauthenticated at the app layer by design; abuse
  control is the WAF rate rule. A session id is a bearer token for *that
  session's plots only* — it grants nothing else.
- The session store is per task. Run `desiredCount = 1` or add a shared
  backend before scaling out (§5).
- Stateful streamable-HTTP (`stateless_http=False`) is not used; if
  elicitation/sampling is ever needed it would additionally require a shared
  event store and single-task pinning.

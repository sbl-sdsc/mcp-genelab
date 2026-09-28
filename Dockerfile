# Dockerfile for mcp-genelab MCP Server
# Runs the MCP GeneLab server with Streamable HTTP transport as ONE shared
# process behind an Application Load Balancer on ECS Fargate:
#
#   MCP client → CloudFront (+WAF) → ALB → ECS Fargate task (this image)
#              → Neo4j Community on EC2 (Bolt 7687, private)
#
# The server exposes 24 MCP tools (22 analysis/utility tools plus the
# create_session / end_session pair) and a `plot://{session_id}/{filename}`
# resource template. Per-user state is keyed on an application-level session
# id (see docs/deployment.md §5) because a shared process has no per-user
# isolation of its own.
#
# HTTP contract (verified against the AWS ECS / ALB documentation):
#   - Platform: linux/arm64. The task definition must declare
#     runtimePlatform: {cpuArchitecture: ARM64, operatingSystemFamily: LINUX}
#     (Fargate platform version 1.4.0+). Build on x86_64 with
#       docker buildx build --platform linux/arm64 -t mcp-genelab .
#   - Host 0.0.0.0, port 8000.
#   - POST /mcp        — the MCP Streamable HTTP endpoint (stateless_http=True,
#                        so any task can serve any request; no sticky sessions).
#   - GET  /healthz    — liveness (no DB). Use for the ECS container healthCheck.
#   - GET  /readyz     — readiness (Neo4j `RETURN 1`, 4 s budget; 503 when the
#                        KG is unreachable). Use for the ALB target-group health
#                        check (ALB health checks are always HTTP GET; the
#                        default matcher is 200).
#   - GET  /metrics    — usage counters, only when MCP_METRICS_ENDPOINT=1.

FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

# Install system dependencies for matplotlib
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the project files
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Install the package and its dependencies
RUN pip install --no-cache-dir .

# Run as an unprivileged user. The server writes nothing to disk in remote
# mode (plots are kept in memory and delivered inline / via plot://), so no
# writable directories are needed beyond a scratch /tmp.
RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp

# Non-secret operational defaults only.
#
# SECURITY: Neo4j credentials (NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD)
# are intentionally NOT set here. In remote transport (streamable-http/http/
# sse) the server FAILS FAST at startup if they are absent — see
# _require_env() in server.py. This prevents building an image that is
# reachable on a public endpoint with a known, baked-in password. Inject the
# credentials at runtime via the `secrets` block of the ECS task definition
# backed by AWS Secrets Manager. See docs/deployment.md §1.
#
# NEO4J_DATABASE is a non-secret identifier, so a default is fine.
#
# The INSTRUCTIONS env var is intentionally NOT set here. When unset,
# server.py uses its built-in DEFAULT_INSTRUCTIONS, which carries the
# SESSION PROTOCOL (create_session first, pass session_id on every call)
# and the full TOOL SELECTION POLICY (ALWAYS-call / NEVER-call routing
# rules for the specialist tools). Setting INSTRUCTIONS here would override
# both with a plain topic summary and degrade routing.
ENV NEO4J_DATABASE="spoke-genelab-v0.3.1"
ENV MCP_TRANSPORT="streamable-http"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="8000"
# Public path prefix when the ALB / CloudFront publish the service under one,
# e.g. MCP_PATH_PREFIX="/kg" → POST /kg/mcp, GET /kg/healthz, /kg/readyz,
# /kg/metrics (ALB cannot rewrite paths). Empty = routes at the root.
ENV MCP_PATH_PREFIX=""
# Operational bounds (safe defaults; override per deployment). See
# docs/deployment.md §3 for tuning guidance under the shared-process model
# (pool_size × number_of_tasks vs. Neo4j's connection ceiling).
ENV MCP_QUERY_TIMEOUT_SECONDS="60"
ENV MCP_MAX_QUERY_ROWS="1000"
ENV MCP_NEO4J_POOL_SIZE="20"
ENV MCP_NEO4J_ACQUISITION_TIMEOUT="30"
ENV MCP_LOG_LEVEL="INFO"
# Session scoping (docs/deployment.md §5). "strict" = every tool except
# create_session requires a valid session_id — the only safe setting for a
# shared process.
ENV MCP_SESSION_POLICY="strict"
ENV MCP_SESSION_IDLE_TTL_SECONDS="3600"
ENV MCP_SESSION_MAX_AGE_SECONDS="28800"
ENV MCP_MAX_SESSIONS="10000"
ENV MCP_SESSION_EVICTION_MIN_IDLE_SECONDS="300"
ENV MCP_MAX_PLOTS_PER_SESSION="8"
ENV MCP_MAX_TOTAL_PLOT_BYTES="268435456"
# Usage metrics (docs/deployment.md §7): JSON usage log on stderr is on by
# default; CloudWatch EMF metrics and the /metrics endpoint are opt-in.
ENV MCP_USAGE_LOG="1"
ENV MCP_METRICS_EMF="0"
ENV MCP_METRICS_ENDPOINT="0"
ENV MCP_READYZ_TIMEOUT_SECONDS="4"
ENV MCP_READYZ_CACHE_SECONDS="2"

EXPOSE 8000

# Liveness probe for `docker run` / compose. ECS ignores this instruction —
# copy the same command into the task definition's container `healthCheck`
# (docs/deployment.md §6). Uses the stdlib so the image needs no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; p='/'+os.environ.get('MCP_PATH_PREFIX','').strip('/'); p='' if p=='/' else p; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000'+p+'/healthz', timeout=3).status == 200 else 1)"]

# Run the MCP server
CMD ["mcp-genelab"]

# Dockerfile for mcp-genelab MCP Server
# Runs the MCP GeneLab server with Streamable HTTP transport for remote
# deployment behind a TLS reverse proxy. The server exposes 22 MCP tools
# plus a `plot://{filename}` resource template for retrieving plot bytes
# via `resources/read`.
#
# AWS Bedrock AgentCore MCP protocol contract (verified against
# docs.aws.amazon.com/bedrock-agentcore .../runtime-mcp-protocol-contract.html):
#   - Platform: ARM64 container (REQUIRED — the runtime rejects amd64 images).
#   - Host: 0.0.0.0
#   - Port: 8000 (MCP servers use 8000, NOT the 8080 used by the "agent"/HTTP
#     protocol contract).
#   - Path: /mcp (POST). There is NO /ping endpoint in the MCP contract — the
#     platform monitors health via the /mcp endpoint itself. 
#   - Transport: stateless streamable-http (stateless_http=True), so the server
#     must accept the platform-injected Mcp-Session-Id header without rejecting it.
#
# The `--platform=linux/arm64` pin below ensures the image is ARM64 even when
# built on an x86_64 host. On Docker without emulation, build with buildx:
#   docker buildx build --platform linux/arm64 -t mcp-genelab .

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

# Non-secret operational defaults only.
#
# SECURITY: Neo4j credentials (NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD)
# are intentionally NOT set here. In remote transport (streamable-http/http/
# sse) the server FAILS FAST at startup if they are absent — see
# _require_env() in server.py. This prevents building an image that is
# reachable on a public endpoint with a known, baked-in password. Inject the
# credentials at runtime via your orchestrator's secret store, e.g. the
# `secrets:` block of an ECS / Bedrock AgentCore task definition backed by
# AWS Secrets Manager. See docs/deployment.md.
#
# NEO4J_DATABASE is a non-secret identifier, so a default is fine.
#
# The INSTRUCTIONS env var is intentionally NOT set here. When unset,
# server.py uses its built-in DEFAULT_INSTRUCTIONS, which carries the
# full TOOL SELECTION POLICY (ALWAYS-call / NEVER-call routing rules for
# the specialist tools). Setting INSTRUCTIONS in this Dockerfile would
# override that policy with a plain topic summary and degrade routing.
ENV NEO4J_DATABASE="spoke-genelab-v0.3.1"
ENV MCP_TRANSPORT="streamable-http"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="8000"
# Operational bounds (safe defaults; override per deployment). See
# docs/deployment.md for tuning guidance under AgentCore's per-microVM model.
ENV MCP_QUERY_TIMEOUT_SECONDS="60"
ENV MCP_MAX_QUERY_ROWS="1000"
ENV MCP_NEO4J_POOL_SIZE="20"
ENV MCP_LOG_LEVEL="INFO"

EXPOSE 8000

# Run the MCP server
CMD ["mcp-genelab"]

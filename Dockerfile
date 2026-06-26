# Dockerfile for mcp-genelab MCP Server
# Runs the MCP GeneLab server with Streamable HTTP transport for remote
# deployment behind a TLS reverse proxy. The server exposes 22 MCP tools
# plus a `plot://{filename}` resource template for retrieving plot bytes
# via `resources/read`.

FROM python:3.12-slim

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

# Default environment variables (override at runtime).
#
# The INSTRUCTIONS env var is intentionally NOT set here. When unset,
# server.py uses its built-in DEFAULT_INSTRUCTIONS, which carries the
# full TOOL SELECTION POLICY (ALWAYS-call / NEVER-call routing rules for
# the 9 specialist tools). Setting INSTRUCTIONS in this Dockerfile would
# override that policy with a plain topic summary and degrade routing.
# To customize, override INSTRUCTIONS at `docker run` time with the FULL
# policy block, not a topic summary.
ENV NEO4J_URI="bolt://neo4j-kg:7687"
ENV NEO4J_USERNAME="neo4j"
ENV NEO4J_PASSWORD="changeme123"
ENV NEO4J_DATABASE="neo4j"
ENV MCP_TRANSPORT="streamable-http"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="8000"

EXPOSE 8000

# Run the MCP server
CMD ["mcp-genelab"]

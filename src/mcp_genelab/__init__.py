"""MCP server for querying Neo4j endpoints."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("mcp_genelab")
except PackageNotFoundError:
    __version__ = "0.0.0"

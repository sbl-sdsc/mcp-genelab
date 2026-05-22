import os
import json
import logging
import re
import base64
import asyncio
from datetime import datetime
from typing import Any, Optional

from . import __version__

import numpy as np
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
from matplotlib_venn import venn2, venn3
from adjustText import adjust_text

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncTransaction,
    READ_ACCESS
)
from pydantic import Field

logger = logging.getLogger("mcp-genelab")
logger.setLevel(logging.DEBUG)


# ===========================================================================
# Output-directory configuration
# ===========================================================================
# The MCP server may run in three different deployment contexts:
#
#   1. AWS Bedrock AgentCore Runtime — server is in an ephemeral Firecracker
#      microVM, container filesystem is NOT reachable by the user. Any file the
#      server writes lives only inside the microVM until session end. There is
#      no network mount, no bind mount, no way for the server to push bytes to
#      the user's local disk directly.
#
#   2. Claude.ai chat UI — server runs on the user's machine via stdio, and
#      Claude's analysis runtime separately mounts /mnt/user-data/outputs/
#      which the LLM's bash_tool can read. Files written there become
#      downloadable via Claude's present_files tool.
#
#   3. Local stdio — server is a child process on the user's own machine.
#      ~/Downloads works because the path resolves to the user's filesystem.
#
# In context (1), the user CANNOT receive files by having the server write to
# its own disk — that disk is unreachable. The only mechanisms that work are:
#   (a) Inline content: ImageContent (PNG) and TextContent (markdown/CSV) come
#       back in the JSON-RPC response, are passed through by AgentCore unchanged,
#       and are rendered by the user's MCP client.
#   (b) Embedded shell commands: a TextContent block carries the base64 image
#       bytes and a copy-paste-ready `python3 -c "..."` command the user (or
#       their LLM-with-bash) can run on their own machine to materialize the
#       file at any path they want.
#
# To let the user specify a save path on their own machine, the new
# `set_output_directory` tool stores a path string in this module-level dict
# keyed by what we have available for session identity. In AgentCore stateless
# mode, FastMCP passes through the Mcp-Session-Id header but we cannot reliably
# read it from inside a tool. So we use a single module-level variable: each
# microVM serves exactly one user session, so a single global is correct for
# AgentCore. For multiplexed deployments (uncommon), users would each call
# `set_output_directory` at the start of their session.
#
# The stored path is used in two ways:
#   - In stdio / local deployments, the server writes the file there directly.
#   - In all deployments, the path is used as the suggested save location in
#     the embedded shell command emitted with every plot.
# ===========================================================================

_USER_OUTPUT_DIR: Optional[str] = None
"""User-specified path string where output files should be saved. Set by
set_output_directory. Used as the suggested save location in embedded shell
commands and (when reachable, i.e. in stdio/local mode) as the actual write
target for the server's own file output."""


def _get_user_output_dir() -> Optional[str]:
    """Return the user-specified output directory if set, else None."""
    return _USER_OUTPUT_DIR


def _set_user_output_dir(path: str) -> None:
    """Set the user-specified output directory. Caller is responsible for
    validation; this just stores the string."""
    global _USER_OUTPUT_DIR
    _USER_OUTPUT_DIR = path


# ---------------------------------------------------------------------------
# Last-plot registry
# ---------------------------------------------------------------------------
# Stores the PNG bytes and user-facing path of the most recently generated
# plot, keyed by the suggested filename. Used by `get_save_script` so the
# user can request a copy-paste-ready Python save script ON DEMAND, rather
# than having every plot response carry one inline (which doubles the
# response size and burns conversation context).
#
# The registry is bounded to MAX_LAST_PLOTS entries (FIFO eviction) so
# memory stays predictable in long sessions. PNG bytes for evicted entries
# are dropped — if the user asks for a save script for an old plot they'll
# need to regenerate it. In AgentCore Runtime each session is its own
# microVM, so the bound is more of a hygiene measure than a hard need.
# ---------------------------------------------------------------------------

_LAST_PLOTS: "dict[str, tuple[bytes, str]]" = {}
"""Map of suggested_filename → (png_bytes, user_facing_path) for recently
generated plots, available for on-demand save-script retrieval via
the `get_save_script` tool."""

MAX_LAST_PLOTS = 8
"""Maximum number of plots to retain bytes for. Older entries are evicted
FIFO. Tuned to cover a typical analysis session (a few volcano plots +
a few Venn diagrams) without bloating memory."""


def _register_plot(suggested_filename: str, png_bytes: bytes, user_facing_path: str) -> None:
    """Record a freshly-generated plot's bytes so a save script can be
    produced on demand later. FIFO-evicts to keep MAX_LAST_PLOTS entries."""
    # If already registered (re-generation of the same plot), update in place
    # rather than evicting; this also preserves insertion order.
    _LAST_PLOTS[suggested_filename] = (png_bytes, user_facing_path)
    while len(_LAST_PLOTS) > MAX_LAST_PLOTS:
        # Pop the oldest entry. In Python 3.7+ dicts preserve insertion order.
        oldest = next(iter(_LAST_PLOTS))
        del _LAST_PLOTS[oldest]


def _lookup_plot(suggested_filename: str) -> "Optional[tuple[bytes, str]]":
    """Return (png_bytes, user_facing_path) for a previously-registered plot,
    or None if the filename isn't in the registry (e.g. it was evicted, or it
    came from a different session)."""
    return _LAST_PLOTS.get(suggested_filename)


def _list_registered_plots() -> "list[str]":
    """Return the suggested filenames currently in the registry, oldest first."""
    return list(_LAST_PLOTS.keys())


def _is_remote_deployment() -> bool:
    """True when this server is running in a transport mode where the
    container's filesystem is not reachable by the user (e.g. AgentCore
    Runtime, any streamable-http deployment). False in local stdio mode.

    In remote deployment, server-side file writes don't deliver files to the
    user — only inline content + embedded shell commands do."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    return transport in ("streamable-http", "http", "sse")


async def _read(tx: AsyncTransaction, query: str, params: dict[str, Any]) -> str:
    """Run a read query and return the records as a JSON string.

    The JSON is computed via json.dumps([r.data() for r in records], default=str),
    which materializes the full result set in memory. This is acceptable because
    Neo4j sessions are not streaming through the MCP boundary anyway — the whole
    result is collected before the tool returns.
    """
    raw_results = await tx.run(query, params)
    eager_results = await raw_results.to_eager_result()
    return json.dumps([r.data() for r in eager_results.records], default=str)


async def _read_with_count(tx: AsyncTransaction, query: str, params: dict[str, Any]) -> tuple[int, str]:
    """Like _read, but also returns the row count alongside the JSON.

    Counting from the records list is O(n) on a Python list — much cheaper than
    re-parsing the serialized JSON string a second time to call len() on it, which
    is what we'd otherwise have to do for the row-count header.
    """
    raw_results = await tx.run(query, params)
    eager_results = await raw_results.to_eager_result()
    records = eager_results.records
    n = len(records)
    return n, json.dumps([r.data() for r in records], default=str)

def _is_write_query(query: str) -> bool:
    """Check if the query is a write query."""
    return (
        re.search(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|ADD|DROP)\b", query, re.IGNORECASE)
        is not None
    )


def _resolve_output_paths(filename_stem: str, extension: str = "csv") -> tuple[str, str, str]:
    """Resolve where output files should be written and how to surface the
    download path to the user. Returns a 3-tuple:
      - write_path: where the server actually writes the file (inside its
        own filesystem; may not be user-reachable in remote deployments)
      - user_facing_path: what to show the user as where they will find the
        file (the user_output_dir + sanitized filename, when set; otherwise
        the same as write_path)
      - download_link: a file:// or computer:// URI suitable for present_files
        / chat-UI download links

    Precedence (highest to lowest):
      1. User-specified path via set_output_directory — used as the user-facing
         save location. When the path is reachable from this process (stdio /
         local deployment), the server also WRITES there. In remote deployment,
         the path is only a suggestion shown to the user in the embedded save
         command; the server still writes its working copy to /tmp.
      2. /mnt/user-data/outputs if it exists — Claude.ai analysis-environment
         convention. The server writes there and uses a `computer://` link.
      3. ~/Downloads — direct local stdio with no Claude analysis env.

    The filename_stem is sanitized to [A-Za-z0-9_-], collapsing repeated
    underscores, and truncated+hashed if longer than 200 chars.
    """
    safe = re.sub(r'[^\w\-]', '_', filename_stem)
    safe = re.sub(r'_+', '_', safe).strip('_') or "results"
    if len(safe) > 200:
        import hashlib
        digest = hashlib.sha1(safe.encode("utf-8")).hexdigest()[:10]
        safe = safe[:180] + "_" + digest
    filename = f'{safe}.{extension}'

    user_dir = _get_user_output_dir()
    if user_dir:
        # The user has declared where they want files. Use that path as the
        # user-facing location regardless of whether we can actually write
        # there ourselves. In remote deployments, the inline ImageContent
        # plus the embedded save command is what actually puts the file on
        # the user's machine.
        user_facing = os.path.join(user_dir, filename)
        if _is_remote_deployment():
            # Server cannot reach user_dir, so write a working copy to a
            # location we KNOW is writable (the microVM's /tmp). The
            # working copy is only used to support inline base64 encoding;
            # the user's actual file comes from the embedded save command.
            write_path = os.path.join('/tmp', filename)
        else:
            # stdio/local mode — user_dir should be reachable; write there.
            write_path = user_facing
        return write_path, user_facing, f"file://{user_facing}"

    # No user-specified path — fall back to legacy behavior.
    if os.path.exists('/mnt/user-data/outputs'):
        path = os.path.join('/mnt/user-data/outputs', filename)
        return path, path, f"computer:///mnt/user-data/outputs/{filename}"

    if _is_remote_deployment():
        # No user dir set AND in remote deployment. Write the server's working
        # copy to /tmp (somewhere we know is writable inside the microVM), but
        # surface a USER-FACING path of ./<filename> — that's where the user's
        # save script will land the file when they run it on their own machine
        # (which they presumably do from a directory of their choice). This is
        # friendlier than dumping the AgentCore microVM's /tmp path on them.
        path = os.path.join('/tmp', filename)
        user_facing = f"./{filename}"
        return path, user_facing, (
            f"file://{user_facing}  "
            f"(No output directory set; the save script defaults to the user's "
            f"current working directory. Call set_output_directory(path='...') "
            f"first to override this.)"
        )

    path = os.path.join(os.path.expanduser('~/Downloads'), filename)
    return path, path, f"file://{path}"


def _render_csv_text(rows: list[dict]) -> Optional[str]:
    """Render a list of dict rows to an in-memory CSV string.

    Returns the CSV text on success, or None for empty/None input. The
    column order is the union of all keys, preserving first-seen ordering
    so pandas/Excel users see a stable layout.

    This helper is paired with `_write_results_csv` in tools that want both:
      - a file on disk (for stdio-mode local users; written via `_write_results_csv`)
      - the CSV text inline in the response (so remote-deployment users can
        copy-and-save without depending on the unreachable container path).

    The two helpers are kept separate so callers can still write a file
    only (legacy behavior) or render only the inline text — without
    duplicating the dict-rows-to-CSV serialization logic.
    """
    if not rows:
        return None
    try:
        import csv as _csv
        import io
        fieldnames: list[str] = []
        seen: set = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        buf = io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Failed to render CSV text: {e}")
        return None


def _write_results_csv(rows: list[dict], filename_stem: str) -> tuple[Optional[str], Optional[str]]:
    """Write a list of dict rows to a CSV file using the standard output dir pattern.

    Returns (user_facing_path, download_link) on success, or (None, None) on failure.

    Behavior depends on deployment mode:
      - Local stdio mode (`_is_remote_deployment()` is False): the server writes
        the CSV to disk at the resolved path so users on their own machine can
        open the file directly.
      - Remote mode (AgentCore Runtime / streamable-http): the server SKIPS the
        disk write — the microVM filesystem isn't reachable by the user, so a
        file in `/tmp` is just leaked I/O. The CSV reaches the user via the
        inline fenced `csv` code block in the response (rendered by every MCP
        client). We still return a suggested user-facing path string and a
        download_link, so the response can name the file with a sensible
        filename for the user to save the inline CSV as.

    The CSV is written using csv.DictWriter, preserving the union of keys
    across all rows (first row's order takes precedence). Empty/None row
    lists produce no file and return (None, None).
    """
    if not rows:
        return None, None
    try:
        write_path, user_facing_path, download_link = _resolve_output_paths(
            filename_stem, extension="csv"
        )
        # In remote mode the inline CSV is the user's only reachable copy.
        # Writing to /tmp inside the microVM accomplishes nothing except
        # consuming temp space, which can leak across stateful sessions.
        # We still need to return the user_facing_path / download_link so the
        # response can present a meaningful suggested filename.
        if _is_remote_deployment():
            return user_facing_path, download_link

        import csv as _csv
        os.makedirs(os.path.dirname(write_path), exist_ok=True)
        # Use the union of keys across all rows (first row's order takes precedence)
        fieldnames: list[str] = []
        seen: set = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with open(write_path, "w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return user_facing_path, download_link
    except Exception as e:
        logger.warning(f"Failed to write CSV for {filename_stem}: {e}")
        return None, None


def _canonical_block(
    table_md: str,
    csv_path: Optional[str],
    csv_link: Optional[str],
    context_label: str = "results table",
    csv_text: Optional[str] = None,
) -> str:
    """Wrap a markdown table with an inline CSV download alongside it.

    The markdown table renders as a human-friendly table. The CSV text, if
    supplied, is wrapped in a fenced ``csv`` code block immediately below
    the table, with a caption naming the suggested filename. Together the
    user gets:
      - a readable table for visual inspection, AND
      - byte-exact CSV text they can copy-and-save, or ask their LLM client
        to write to disk via local file access (filesystem MCP, Cline,
        Cursor, etc.).

    Why inline CSV text and not a `_CSV: /path/...` reference:
      In remote deployments (AgentCore Runtime), the server runs in an
      ephemeral microVM whose filesystem is not reachable by the user. A
      `_CSV: /tmp/foo.csv` line would point to a path the user can't open.
      Inline CSV text is plain markdown and is delivered to every MCP
      client, ephemeral container or not.

    Callers that only pass `csv_path` (no `csv_text`) fall through to a
    `_CSV: <path>` reference line for local-mode compatibility.
    """
    out = table_md
    if csv_text:
        # Suggested filename: prefer the basename of csv_path when available;
        # fall back to a generic "data.csv" otherwise. This is what appears
        # in the caption.
        suggested_name = os.path.basename(csv_path) if csv_path else "data.csv"
        out += (
            f"\n\n**CSV (copy-and-save as `{suggested_name}`):**\n"
            f"```csv\n{csv_text}```\n"
        )
    elif csv_path:
        # Path-reference fallback for callers that don't supply inline CSV text.
        out += f"\n\n_CSV: `{csv_path}`_"
    return out


def _make_save_instructions(
    user_facing_path: str,
    suggested_filename: str,
    png_size_bytes: int,
) -> str:
    """Build a markdown block listing the available retrieval paths for
    the PNG bytes of a previously generated plot.

    The response references the canonical resource URI (plot://<filename>)
    and the fetch_plot tool. Both retrieve the bytes from the in-memory
    plot registry via a separate request, independent of this tool's
    response. The registry holds the last MAX_LAST_PLOTS plots
    (FIFO eviction), so resource URIs remain stable across calls within
    a session and a failed fetch can be retried without re-rendering.

    Args:
        user_facing_path: where the user has asked files to be saved.
            Used in the "ask your LLM client to save it" guidance, since
            an LLM client with filesystem access needs a target path.
        suggested_filename: the bare filename (no directory). Used to
            construct the plot:// URI and the fetch_plot call.
        png_size_bytes: size of the PNG in bytes, shown to the user so
            they know what to expect.
    """
    size_kb = max(1, png_size_bytes // 1024)
    return (
        f"\n---\n"
        f"### Download options for `{suggested_filename}` ({size_kb} KB)\n\n"
        f"The plot above is included inline in this response. To save it to "
        f"your local computer at `{user_facing_path}`, choose whichever option "
        f"works in your environment:\n\n"
        f"**Option A — Right-click the image above** and choose \"Save Image As…\" "
        f"(works in most chat UIs that render MCP images inline). This is the "
        f"simplest path for most users.\n\n"
        f"**Option B — Ask your LLM client to save it.** If your client has a "
        f"filesystem MCP server, write tool, or local shell access (Cline, "
        f"Cursor, Claude Code, the filesystem MCP server), ask it to save the "
        f"inline PNG to `{user_facing_path}`. If the inline image is missing "
        f"or didn't render cleanly, the LLM can re-fetch the canonical bytes "
        f"without re-rendering by calling `fetch_plot(filename=\"{suggested_filename}\")` "
        f"or reading the resource at `plot://{suggested_filename}`.\n\n"
        f"**Option C — Re-fetch via resource URI.** Clients that support MCP "
        f"resources can fetch the canonical PNG bytes via "
        f"`resources/read` on `plot://{suggested_filename}`. This is the most "
        f"robust path because the fetch is a separate request that can be "
        f"retried if the original response was corrupted in transit.\n"
    )



def _make_save_hint(
    user_facing_path: str,
    suggested_filename: str,
    png_size_bytes: int,
) -> str:
    """Build a SHORT (~400-byte) download hint for a freshly generated plot.

    This is the default download guidance every plot tool emits. It points
    the user at the three save mechanisms (right-click, LLM filesystem tool,
    on-demand Python script) WITHOUT embedding the full base64 payload —
    embedding it inline with every plot response would roughly double the
    response size and consume conversation context faster than necessary.
    The embedded-script form is delivered separately by the `get_save_script`
    tool only when the user explicitly asks for it.

    Args:
        user_facing_path: where the user has asked files to be saved.
        suggested_filename: bare filename (no directory), used to identify
            this plot when the user later asks `get_save_script` for it.
        png_size_bytes: size of the PNG in bytes, shown to the user so they
            know what to expect.
    """
    size_kb = max(1, png_size_bytes // 1024)
    return (
        f"\n---\n"
        f"### Save `{suggested_filename}` ({size_kb} KB)\n\n"
        f"The plot above is included inline. To save it to "
        f"`{user_facing_path}` on your computer, use any of these:\n\n"
        f"- **Right-click the image above** → \"Save Image As…\" (works in "
        f"every chat UI that renders MCP images).\n"
        f"- **Ask your LLM client to save it** if it has filesystem access "
        f"(filesystem MCP server, Cline, Cursor, Claude Code). If the inline "
        f"image is missing or rendered poorly, the LLM can call "
        f"`fetch_plot(filename=\"{suggested_filename}\")` or read "
        f"`plot://{suggested_filename}` to re-fetch the canonical bytes "
        f"without re-rendering.\n"
        f"- **Get full save options:** call `get_save_script("
        f"filename=\"{suggested_filename}\")` to receive a markdown block "
        f"describing every available retrieval path (right-click, "
        f"resource URI, fetch_plot).\n"
    )


def create_mcp_server(neo4j_driver: AsyncDriver, database: str = "neo4j", instructions: str = "", host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    mcp: FastMCP = FastMCP("mcp-genelab", dependencies=["neo4j", "pydantic"], instructions=instructions, host=host, port=port, stateless_http=True)

    # Plot resource layer: every plot generated by create_volcano_plot or
    # create_venn_diagram is held in _LAST_PLOTS and exposed as an MCP
    # resource under the plot://<suggested_filename> URI. Clients fetch the
    # bytes via `resources/read` (or via the fetch_plot tool below) in a
    # separate request from the tool call that produced the plot, so a
    # failed fetch can be retried without re-running Cypher + matplotlib.
    # The URI's filename is the same key the tools use to register and
    # look up plots, so resource URIs are stable within a session.

    @mcp.resource(
        "plot://{filename}",
        name="plot",
        title="Plot PNG (Volcano / Venn)",
        description=(
            "Binary PNG bytes for a plot previously generated by "
            "create_volcano_plot or create_venn_diagram. The {filename} "
            "parameter is the suggested_filename surfaced in the plot "
            "tool's response (e.g. 'venn_expression_2way_OSD-244.png'). "
            "The server keeps the last 8 plots in memory; older plots "
            "are evicted FIFO. Retrieve the same plot again by re-fetching "
            "this URI — no need to re-render."
        ),
        mime_type="image/png",
    )
    def plot_resource(filename: str) -> bytes:
        """Return the PNG bytes for a registered plot, or raise ValueError if
        the plot is not in the registry. FastMCP base64-encodes the bytes into
        a BlobResourceContents automatically and delivers them via resources/read.

        Notes:
          - This is a READ-ONLY accessor. It does not mutate the registry.
          - Repeated reads of the same URI return the same bytes (idempotent).
          - If the plot has been evicted by the FIFO bound, the caller gets a
            clear error and can re-run the plot tool to regenerate it.
        """
        looked_up = _lookup_plot(filename)
        if looked_up is None:
            available = ", ".join(_list_registered_plots()) or "(none)"
            raise ValueError(
                f"No plot named {filename!r} is in the registry. "
                f"Available plots: {available}. The registry holds the "
                f"last {MAX_LAST_PLOTS} plots; older entries are evicted."
            )
        png_bytes, _user_facing_path = looked_up
        return png_bytes

    # fetch_plot: companion to the plot:// resource for clients that don't
    # surface MCP resources well. Returns the same bytes as an
    # EmbeddedResource via the tool interface. Pure registry lookup + encode
    # — no Cypher, no matplotlib — so it's safe to call repeatedly and the
    # canonical bytes are stable across calls within a session.

    @mcp.tool(
        annotations={
            "title": "Fetch Plot Bytes (registry lookup)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def fetch_plot(
        filename: str = Field(
            ...,
            description=(
                "Suggested filename of a previously generated plot "
                "(e.g. 'venn_expression_2way_OSD-244.png'). Look at the "
                "most recent create_volcano_plot or create_venn_diagram "
                "response — the filename appears in the 'Save' block. "
                "The server keeps the last 8 plots in memory."
            ),
        ),
    ) -> list[types.Content]:
        """Re-fetch the PNG bytes of a previously generated plot.

        WHEN TO USE THIS TOOL:
        Call this when you need the canonical PNG bytes for a plot that's
        already been generated. Common cases:
          - The inline image in the original plot tool response was
            corrupted or didn't render in the client.
          - You need the bytes for a downstream step (e.g. embedding in a
            transcript, saving to a side file, base64-encoding for an
            external API).
          - The user asked to see the plot again without re-running the
            analysis.

        WHAT THIS TOOL DOES NOT DO:
        It does NOT re-render the plot. It returns the same bytes that
        were registered when the plot was originally generated. If the
        underlying data has changed, re-run the plot tool itself
        (create_volcano_plot, create_venn_diagram); don't expect fetch_plot
        to pick up new data.

        EQUIVALENT RESOURCE URI:
        This tool returns the same bytes that `resources/read` would return
        for `plot://<filename>`. Use whichever path your client supports
        better — resources for clients that render them well, this tool for
        clients that don't.

        REGISTRY BOUNDS:
        The registry holds the last 8 plots (FIFO eviction). Older plots
        return a clear "not in registry" error and need to be regenerated
        by re-running the source plot tool.
        """
        if not filename or not filename.strip():
            return [types.TextContent(
                type="text",
                text=(
                    "Error: `filename` is required. Pass the suggested "
                    "filename surfaced by the most recent plot tool response."
                ),
            )]

        looked_up = _lookup_plot(filename.strip())
        if looked_up is None:
            available = _list_registered_plots()
            hint = (
                "Available plots: " + ", ".join(f"`{p}`" for p in available)
                if available
                else "No plots are currently in the registry."
            )
            return [types.TextContent(
                type="text",
                text=(
                    f"No plot named `{filename}` is in the registry "
                    f"(only the last {MAX_LAST_PLOTS} plots are retained).\n\n"
                    f"{hint}\n\n"
                    f"Re-run create_volcano_plot or create_venn_diagram with "
                    f"the same arguments to regenerate."
                ),
            )]

        png_bytes, user_facing_path = looked_up
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        size_kb = max(1, len(png_bytes) // 1024)

        # Return as EmbeddedResource so the client can render the bytes
        # inline using the resource UI, with the plot:// URI as the canonical
        # reference. We also include a short TextContent summary so the LLM
        # gets a textual confirmation of what was returned.
        return [
            types.TextContent(
                type="text",
                text=(
                    f"Returning PNG bytes for `{filename}` ({size_kb} KB). "
                    f"Canonical URI: `plot://{filename}`. "
                    f"Local save path (if filesystem write is available): "
                    f"`{user_facing_path}`."
                ),
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.BlobResourceContents(
                    uri=f"plot://{filename}",
                    mimeType="image/png",
                    blob=b64,
                ),
            ),
        ]

    @mcp.tool(
        annotations={
            "title": "Get Neo4j Schema",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_neo4j_schema() -> list[types.TextContent]:
        """List all nodes, their attributes and their relationships to other nodes in the neo4j database.
        If this fails with a message that includes "Neo.ClientError.Procedure.ProcedureNotFound"
        suggest that the user install and enable the APOC plugin.
        """

        get_schema_query = """
call apoc.meta.data() yield label, property, type, other, unique, index, elementType
where elementType = 'node' and not label starts with '_'
with label, 
    collect(case when type <> 'RELATIONSHIP' then [property, type + case when unique then " unique" else "" end + case when index then " indexed" else "" end] end) as attributes,
    collect(case when type = 'RELATIONSHIP' then [property, head(other)] end) as relationships
RETURN label, apoc.map.fromPairs(attributes) as attributes, apoc.map.fromPairs(relationships) as relationships
"""

        try:
            async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                results_json_str = await session.execute_read(
                    _read, get_schema_query, dict()
                )

                logger.debug(f"Schema query returned {len(results_json_str)} bytes")
                logger.debug(results_json_str)

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving schema: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    @mcp.tool(
        annotations={
            "title": "Set Output Directory",
            "readOnlyHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def set_output_directory(
        path: str = Field(
            ...,
            description=(
                "Absolute path on the USER's local machine where output files "
                "(volcano plots, Venn diagrams, CSV exports) should be saved. "
                "Examples: '/Users/jane/Downloads', '/home/jane/Downloads', "
                "'C:/Users/Jane/Downloads' (forward slashes work on Windows too "
                "with pathlib). Avoid trailing path separators."
            ),
        ),
    ) -> list[types.TextContent]:
        """Configure the output directory for files generated by this MCP server.

        WHEN TO USE THIS TOOL:
        Call this tool at the START of a session, before any tool that produces
        a downloadable file (create_volcano_plot, create_venn_diagram, or any
        tool that emits a CSV). The path you specify is where the user wants
        finished files to end up on THEIR OWN local machine.

        WHAT IT DOES:
        Stores the provided path string in the server's session-scoped state.
        Every subsequent file-producing tool uses this path:
          - As the user-facing save location shown in the response summary.
          - As the default target path embedded in the download-helper script
            included with every plot.
          - If the server is running locally (stdio transport), the server
            ALSO writes the file directly to this path.

        IMPORTANT — DEPLOYMENT REALITY:
        When this MCP server is deployed on AWS Bedrock AgentCore Runtime (or
        any other remote container service), the server runs in an ephemeral
        microVM whose filesystem is NOT reachable from the user's machine. The
        server CANNOT write a file directly to the user's local disk over the
        network — no MCP server in a container can. The path you set here is
        a suggestion that propagates through the response in three ways, any
        of which lets the user save the file locally:
          (a) The plot is returned inline as ImageContent and renders in any
              MCP client that displays images (Claude Desktop, Cursor, Cline,
              Claude.ai web, ChatGPT custom GPTs, Gemini, etc.). The user can
              right-click and "Save Image As" — most clients default to the
              user's Downloads folder, which is usually what was asked for.
          (b) The path you set appears in a download-helper script in the
              response. LLM clients with local filesystem access (Claude Code,
              Cursor, Cline, Claude Desktop with a filesystem MCP server) can
              run this script automatically to write the file at exactly this
              path.
          (c) Users without LLM-mediated filesystem access can copy the
              script into a file and run `python save_plot.py` themselves on
              their own machine. This is cross-platform (macOS/Linux/Windows)
              and uses no third-party packages.

        VALIDATION:
        The path is stored as-is (the server does not verify it exists on the
        user's machine — it has no way to do so from inside a container). If
        you pass a relative path or an obviously malformed string the server
        will accept it; the failure will surface when the user tries to save
        the file locally.
        """
        if not path or not path.strip():
            return [types.TextContent(
                type="text",
                text="Error: path must be a non-empty string.",
            )]

        # Strip whitespace and normalize trailing separators. Don't expanduser
        # here — ~ should be expanded by whatever client/script ultimately
        # writes the file, since the server's home directory is inside the
        # AgentCore microVM (irrelevant to the user).
        cleaned = path.strip().rstrip('/').rstrip('\\')
        _set_user_output_dir(cleaned)

        deployment_mode = (
            "remote (AgentCore / streamable-http)"
            if _is_remote_deployment() else "local (stdio)"
        )
        logger.info(f"Output directory set to: {cleaned} (deployment: {deployment_mode})")

        if _is_remote_deployment():
            advisory = (
                f"\n\n**Deployment note:** This MCP server runs in an ephemeral "
                f"container that cannot directly write to your local "
                f"filesystem. Every plot tool will:\n"
                f"  1. Return the image inline (renders in chat — right-click "
                f"to Save As, which typically goes to your Downloads folder).\n"
                f"  2. Include a short save hint pointing at `{cleaned}`.\n"
                f"  3. Make the plot retrievable via `get_save_script("
                f"filename=...)`, which returns a self-contained Python "
                f"script that writes the plot to `{cleaned}` on your machine "
                f"when run with `python save_plot.py`.\n\n"
                f"Tables produced by the data tools (find_differentially_*, "
                f"find_common_*, get_study_info) are also returned with their "
                f"CSV text inline in the response, so you can copy-paste or "
                f"have your LLM client save them locally."
            )
        else:
            advisory = (
                "\n\nThe server is running locally (stdio transport), so it "
                "will write files directly to that path. Each plot tool will "
                "also return the image inline so you can view it immediately "
                "in the chat."
            )

        return [types.TextContent(
            type="text",
            text=(
                f"Output directory set to: `{cleaned}`\n"
                f"Files generated by this session will be saved there." + advisory
            ),
        )]

    @mcp.tool(
        annotations={
            "title": "Get Output Directory",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_output_directory() -> list[types.TextContent]:
        """Return the currently-configured output directory for this session.

        Returns the path previously set by set_output_directory, or a note
        explaining that no path has been set yet (and how to set one).
        """
        current = _get_user_output_dir()
        if current:
            return [types.TextContent(
                type="text",
                text=f"Current output directory: `{current}`",
            )]
        return [types.TextContent(
            type="text",
            text=(
                "No output directory has been set for this session. "
                "Call `set_output_directory(path='...')` with the absolute "
                "path on your local machine where you want generated files "
                "(volcano plots, Venn diagrams, CSVs) to be saved."
            ),
        )]

    @mcp.tool(
        annotations={
            "title": "Get Save Script for a Plot",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_save_script(
        filename: Optional[str] = Field(
            None,
            description=(
                "Suggested filename of a recently generated plot (e.g. "
                "'volcano_plot_OSD-244_expression_30_days_vs_60_days.png'). "
                "Look at the most recent create_volcano_plot or "
                "create_venn_diagram response — the filename appears in the "
                "'Save' block. Omit this parameter to list the filenames of "
                "all plots currently available in the registry."
            ),
        ),
    ) -> list[types.TextContent]:
        """Return guidance for saving a previously generated plot to the
        user's local filesystem.

        WHEN TO USE THIS TOOL:
        Call this only when the user explicitly asks for save guidance for a
        specific plot — for example, "how do I save the volcano plot to my
        Downloads folder?" or "I can't right-click — give me another way to
        save the plot." By default plot tools (create_volcano_plot,
        create_venn_diagram) return only a SHORT save hint to keep response
        sizes manageable. The detailed save options are delivered on demand
        via this tool, so they don't burn conversation context unless the
        user actually wants them.

        HOW IT WORKS:
        Each plot tool registers the PNG bytes of the plot it generated in
        a per-session registry (up to the last 8 plots; older entries are
        evicted FIFO). Calling this tool with the plot's suggested filename
        returns a markdown block with three save options:

          - Option A (right-click save on the inline image)
          - Option B (ask your LLM client to save via filesystem access,
            optionally re-fetching the canonical bytes through fetch_plot
            or the plot:// resource if the inline image was corrupted)
          - Option C (fetch the canonical PNG bytes via resources/read on
            the plot:// URI for clients that support MCP resources)

        The response itself contains no PNG bytes — only references to the
        plot:// resource URI and the fetch_plot tool, both of which deliver
        the bytes through a separate request.

        LISTING AVAILABLE PLOTS:
        Call this tool with no `filename` argument to list the filenames
        of all plots currently held in the registry. Useful when the user
        asks for save guidance but doesn't remember which plot they want.
        """
        # Listing mode: no filename → return the catalogue.
        if not filename or not filename.strip():
            available = _list_registered_plots()
            if not available:
                return [types.TextContent(
                    type="text",
                    text=(
                        "No plots are currently available for save-script "
                        "retrieval in this session. Generate a plot first "
                        "(create_volcano_plot or create_venn_diagram) and "
                        "then call this tool again with the plot's filename."
                    ),
                )]
            bullets = "\n".join(f"  - `{f}`" for f in available)
            return [types.TextContent(
                type="text",
                text=(
                    f"Plots currently available for save-script retrieval "
                    f"(call `get_save_script(filename=...)` to receive the "
                    f"Python save script for any of these):\n\n{bullets}"
                ),
            )]

        # Lookup mode: caller specified a filename. Resolve from the registry.
        looked_up = _lookup_plot(filename.strip())
        if looked_up is None:
            available = _list_registered_plots()
            if not available:
                hint = (
                    "No plots are currently in the registry. Generate one "
                    "first (create_volcano_plot or create_venn_diagram)."
                )
            else:
                bullets = "\n".join(f"  - `{f}`" for f in available)
                hint = (
                    f"Available filenames:\n\n{bullets}\n\n"
                    f"If the plot you want isn't listed, it may have been "
                    f"evicted from the registry (only the last "
                    f"{MAX_LAST_PLOTS} plots are retained). Regenerate the "
                    f"plot and try again."
                )
            return [types.TextContent(
                type="text",
                text=(
                    f"No plot named `{filename}` is in the registry.\n\n"
                    f"{hint}"
                ),
            )]

        png_bytes, user_facing_path = looked_up
        # The save instructions now reference the canonical plot:// resource
        # URI rather than embedding the base64 payload inline. This keeps the
        # response small (~1 KB regardless of PNG size) and gives the client
        # a retry-safe path to the bytes via resources/read or fetch_plot.
        return [types.TextContent(
            type="text",
            text=_make_save_instructions(user_facing_path, filename, len(png_bytes)),
            mimeType="text/markdown",
        )]

    @mcp.tool(
        annotations={
            "title": "Cypher Query (fallback only)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def query(
        query: str = Field(..., description="The Cypher query to execute."),
        params: Optional[dict[str, Any]] = Field(
            None, description="The parameters to pass to the Cypher query."
        ),
    ) -> list[types.TextContent]:
        """FALLBACK Cypher executor. Use this ONLY when no specialist tool covers the question.

        DO NOT call this tool when the question matches one of these patterns —
        call the named tool instead, EVEN IF you think Cypher would be more flexible:

          - "up/down-regulated genes", "DEGs", "differential expression",
            "log2fc rankings", "top N up/downregulated genes for assay X"
                → find_differentially_expressed_genes(assay_id=...)
          - "hyper/hypo-methylated regions", "DMRs", "methylation_diff",
            "hypermethylated promoter regions", "all DMRs passing q<0.05",
            pooling methylation across replicate assays
                → find_differentially_methylated_regions(assay_id=...)
          - "differentially abundant organisms", DESeq2/ANCOM-BC abundance,
            "increased/decreased abundance for assay X"
                → find_differentially_abundant_organisms(assay_id=...)
          - study metadata, what assays a study has, study factors/materials,
            project title, description, host organism/strain
                → get_study_info(study_id=...)
          - factor-pair → assay-id resolution (e.g. "give me the assay for
            Space Flight,Carcass vs Ground Control,Carcass in OSD-48")
                → select_assays(study_id=..., selection=...)
          - common DEGs/DMRs/DA organisms across multiple assays, intersections
                → find_common_differentially_expressed_genes(assay_ids=[...])
                → find_common_differentially_methylated_regions(assay_ids=[...])
                → find_common_differentially_abundant_organisms(assay_ids=[...])
          - DE genes overlapping DM regions, expression-methylation coupling,
            classical epigenetic silencing analysis
                → find_common_de_genes_overlapping_dm_regions(...)
          - listing nodes / properties / relationships in the KG
                → get_neo4j_schema(), get_node_metadata(), get_relationship_metadata()

        If you only have a study identifier and need assay IDs, call
        `select_assays` or `get_study_info` FIRST; do not write Cypher to
        discover assay IDs.

        The specialist tools return formatted markdown tables AND an inline CSV
        side-channel that this `query` tool does not produce. Falling back to
        `query` for the categories above also loses the parallel multi-query
        execution, the method-aware significance filtering (DESeq2 adj_p_value
        vs. ANCOM-BC q_value), the region-vs-gene aggregation, and the
        MethylationRegion location filter plumbing the specialists already
        implement correctly.

        Only call this tool for ad-hoc Cypher that the specialist tools cannot
        express: schema exploration, custom multi-hop traversals, exotic
        aggregations, or one-off counts the specialists don't expose.

        OUTPUT FORMAT - CRITICAL:
        The response begins with a `total_rows: N` header followed by `rows:` and then the
        JSON array of records. When reporting counts to the user (e.g. "how many
        hypermethylated regions are there"), ALWAYS use `total_rows`, NEVER count rows
        visible in the JSON array. Large results may be truncated by the MCP transport
        and stored to a side-file, leaving only a partial preview of the array — but the
        `total_rows` count is computed server-side from the materialized result before
        any truncation and is always accurate. For aggregate queries (`RETURN count(*)`)
        `total_rows` will be 1 and the count itself lives in the row's `count(*)` field.

        EDGE PROPERTIES - CRITICAL:
        Many relationships in this knowledge graph have properties stored as edge attributes (data ON the relationship itself).

        MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG: log2fc, adj_p_value, group_mean_1, group_stdev_1, group_mean_2, group_stdev_2
        MEASURED_DIFFERENTIAL_METHYLATION_ASmMR: methylation_diff, q_value, group_mean_1, group_stdev_1, group_mean_2, group_stdev_2
        MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO: log2fc, lnfc, q_value, adj_p_value, group_mean_1, group_stdev_1, group_mean_2, group_stdev_2

        ABUNDANCE EDGE - METHOD-AWARE FIELDS (CRITICAL for correct queries):
        The MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO edge stores different fields depending
        on which differential analysis method produced the row. Use `log2fc` (not `lnfc`)
        for direction and ranking, since it is populated for BOTH methods:

          - DESeq2 edges:  log2fc populated, lnfc = null,        adj_p_value populated, q_value = null
          - ANCOM-BC edges: log2fc populated, lnfc populated,    adj_p_value = null,    q_value populated

        To filter by significance in a method-agnostic way, OR both fields together:
          WHERE r.log2fc > 0
            AND (
              (r.adj_p_value IS NOT NULL AND r.adj_p_value <= 0.05)
              OR (r.q_value IS NOT NULL AND r.q_value <= 0.05)
            )

        Filtering on `r.lnfc > 0` will silently drop every DESeq2 row because Cypher's
        `null > 0` evaluates to null and fails the WHERE clause. If you want to use lnfc
        as an ANCOM-BC-specific magnitude filter alongside log2fc-based direction, write
        the clause defensively:
          AND (r.lnfc IS NULL OR abs(r.lnfc) >= 0.5)
        This applies the lnfc threshold only to rows where lnfc is populated (ANCOM-BC)
        and leaves DESeq2 rows untouched.

        RELATIONSHIP DIRECTIONS (always use directed arrows in queries):
        (Assay)-[:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(MGene)
        (Assay)-[:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->(MethylationRegion)
        (Assay)-[:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->(Organism)
        (MGene)-[:METHYLATED_IN_MGmMR]->(MethylationRegion)
        (Study)-[:PERFORMED_SpAS]->(Assay)
        (Mission)-[:CONDUCTED_MIcS]->(Study)

        NODE PROPERTIES - CRITICAL (use EXACTLY these property names, no others exist):
        MGene:             identifier, symbol, name, organism, taxonomy
        MethylationRegion: identifier, name, chromosome, start, end, in_promoter, in_exon, in_intron, dist_to_feature
        Assay:             identifier, name, technology, measurement, differential_analysis_method,
                           factors_1, factors_2, material_1, material_2, material_id_1, material_id_2,
                           material_name_1, material_name_2, factor_space_1, factor_space_2
        Study:             identifier, name, project_title, project_type, description, organism, taxonomy,
                           host_organism, host_strain
        Mission:           identifier, name, flight_program, space_program, start_date, end_date
        Organism:          identifier, name

        COMMON PROPERTY NAME MISTAKES - NEVER USE THESE:
        WRONG: mg.gene_symbol  RIGHT: mg.symbol
        WRONG: mg.gene_name    RIGHT: mg.name
        WRONG: g.gene_symbol   RIGHT: g.symbol
        WRONG: g.gene_name     RIGHT: g.name
        """

        if _is_write_query(query):
            return [types.TextContent(
                type="text",
                text="Error: Only read (MATCH) queries are allowed. Write keywords like "
                     "MERGE, CREATE, SET, DELETE, REMOVE, and ADD are blocked.",
            )]

        # Coerce None → {} to avoid passing a null parameter dict downstream.
        # The Neo4j Python driver accepts None for the parameters argument, but {} is
        # safer and clearer and matches what every other tool in this file passes.
        params = params or {}

        try:
            async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                total_rows, results_json_str = await session.execute_read(_read_with_count, query, params)

            logger.debug(f"Read query returned {total_rows} rows ({len(results_json_str)} bytes)")

            # Putting `total_rows` at the very top of the response, BEFORE the row
            # data, guarantees the count survives any downstream truncation: even
            # when only a few hundred bytes of preview reach the assistant, the
            # count is always in those bytes. The count is computed server-side
            # from the materialized result (one O(n) pass, not a second JSON parse)
            # so it's always exact.
            count_header = f"total_rows: {total_rows}\nrows:\n"

            return [types.TextContent(
                type="text",
                text=count_header + results_json_str,
            )]

        except Exception as e:
            logger.error(f"Database error executing query: {e}\n{query}\n{params}")
            return [
                types.TextContent(type="text", text=f"Error: {e}\n{query}\n{params}")
            ]

    @mcp.tool(
        annotations={
            "title": "Get Node Metadata",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_node_metadata() -> list[types.TextContent]:
        """Get metadata for all nodes from MetaNode nodes in the knowledge graph."""

        metadata_query = """
        MATCH (m:MetaNode)
        RETURN m.nodeName as nodeName, m
        ORDER BY m.nodeName
        """

        try:
            async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                results_json_str = await session.execute_read(
                    _read, metadata_query, {}
                )

                logger.debug(f"Metadata query for all nodes returned {len(results_json_str)} characters")

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving metadata for all nodes: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]


    @mcp.tool(
        annotations={
            "title": "Get Relationship Metadata",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_relationship_metadata() -> list[types.TextContent]:
        """Get descriptions of properties of all relationships in the knowledge graph."""

        metadata_query = """
        MATCH (n1)-[r:MetaRelationship]->(n2)
        WITH n1, r, n2, properties(r) as allProps
        RETURN n1.nodeName as node1, 
               r.relationshipName as relationship, 
               n2.nodeName as node2,
               apoc.map.removeKeys(allProps, ['to', 'from']) AS properties
        """

        try:
            async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                results_json_str = await session.execute_read(
                    _read, metadata_query, {}
                )

                logger.debug(f"Relationship metadata query returned {len(results_json_str)} characters")

                # If MetaRelationship doesn't exist or returns empty, try a fallback approach
                if results_json_str == "[]":
                    logger.debug("MetaRelationship query returned empty, trying fallback to get relationship types")

                    # Get all relationship types in the database
                    fallback_query = """
                    CALL apoc.meta.relTypeProperties()
                    YIELD relType, propertyName, propertyTypes, mandatory
                    WITH relType, collect({propertyName: propertyName,
                                           propertyTypes: propertyTypes}) as properties
                    ORDER BY relType
                    RETURN relType, properties
                    """

                    results_json_str = await session.execute_read(
                        _read, fallback_query, {}
                    )
                    
                    # If that also fails, try using APOC if available
                    if results_json_str == "[]":
                        logger.debug("Basic relationship types query returned empty, trying APOC meta approach")
                        
                        apoc_query = """
                        CALL apoc.meta.graph() YIELD relationships
                        UNWIND relationships as rel
                        WITH rel.type as relType, 
                             keys(rel.properties) as propertyNames,
                             [prop in keys(rel.properties) | rel.properties[prop]] as propertyTypes
                        RETURN relType, propertyNames, propertyTypes
                        ORDER BY relType
                        """
                        
                        results_json_str = await session.execute_read(
                            _read, apoc_query, {}
                        )

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving relationship metadata: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    @mcp.tool(
        annotations={
            "title": "Get Study Info (metadata + assay inventory)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_study_info(
        study_id: str = Field(..., description="Study identifier (e.g., 'OSD-267')")
    ) -> list[types.TextContent]:
        """USE THIS TOOL (not the `query` tool) for any study-metadata question:
        "what assays does study X have", "tell me about OSD-267", project title,
        description, host organism, missions, factor space, or any inventory of
        a study's assays. Falling back to `query` for these reproduces the work
        this tool already does and loses the formatted markdown + inline CSV
        side-channel.

        Return detailed information about a study, including its assays with their technology and measurement types.

        This tool queries the GeneLab KG for:
          1) Study metadata (name, project title, description, organism, etc.)
          2) All assays for the study with their technology, measurement type, differential analysis method,
             factors, and materials
          3) Associated missions

        For example, OSD-267 should return that it has both 16S and ITS amplicon sequencing data.

        """
        
        study_cypher = """
        MATCH (s:Study {identifier: $study_id})
        OPTIONAL MATCH (m:Mission)-[:CONDUCTED_MIcS]->(s)
        RETURN s.identifier AS identifier,
               s.name AS name,
               s.project_title AS project_title,
               s.project_type AS project_type,
               s.description AS description,
               s.organism AS organism,
               s.taxonomy AS taxonomy,
               s.host_organism AS host_organism,
               s.host_strain AS host_strain,
               collect(DISTINCT m.name) AS missions
        """
        
        assay_cypher = """
        MATCH (s:Study {identifier: $study_id})-[:PERFORMED_SpAS]->(a:Assay)
        RETURN a.identifier AS assay_id,
               a.name AS name,
               a.technology AS technology,
               a.measurement AS measurement,
               a.differential_analysis_method AS analysis_method,
               coalesce(a.factors_1, []) AS factors_1,
               coalesce(a.factors_2, []) AS factors_2,
               a.material_1 AS material_1,
               a.material_2 AS material_2
        ORDER BY a.technology, a.measurement, assay_id
        """
        
        try:
            # Run the two independent reads in parallel — they don't share state and
            # each opens its own session, so they can execute concurrently instead of
            # serially. On a typical Neo4j over localhost this halves the latency of
            # the tool (two ~5ms RTTs in parallel ≈ 5ms total).
            async def _study_read():
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, study_cypher, {"study_id": study_id})
            async def _assay_read():
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, assay_cypher, {"study_id": study_id})

            study_json_str, assay_json_str = await asyncio.gather(_study_read(), _assay_read())

            study_data = json.loads(study_json_str)
            assays = json.loads(assay_json_str)
            
            if not study_data:
                return [types.TextContent(type="text", text=f"No study found with identifier '{study_id}'.")]
            
            s = study_data[0]
            
            lines = []
            lines.append(f"## Study: {s.get('identifier', 'N/A')}\n")
            lines.append(f"**Project Title:** {s.get('project_title', 'N/A')}")
            lines.append(f"**Project Type:** {s.get('project_type', 'N/A')}")
            lines.append(f"**Organism:** {s.get('organism', 'N/A')} (Taxonomy: {s.get('taxonomy', 'N/A')})")
            
            if s.get('host_organism'):
                lines.append(f"**Host Organism:** {s.get('host_organism', 'N/A')}")
            if s.get('host_strain'):
                lines.append(f"**Host Strain:** {s.get('host_strain', 'N/A')}")
            
            missions = s.get('missions', [])
            missions = [m for m in missions if m]  # filter nulls
            if missions:
                lines.append(f"**Mission(s):** {', '.join(missions)}")
            
            if s.get('description'):
                desc = s['description']
                if len(desc) > 500:
                    desc = desc[:500] + "..."
                lines.append(f"\n**Description:** {desc}")
            
            lines.append("")
            
            # Summarize assay technologies
            if assays:
                # Group by technology
                tech_summary = {}
                for a in assays:
                    tech = a.get('technology', 'Unknown')
                    meas = a.get('measurement', 'Unknown')
                    key = f"{meas} – {tech}"
                    if key not in tech_summary:
                        tech_summary[key] = 0
                    tech_summary[key] += 1

                lines.append(f"### Data Types ({len(assays)} total assays)\n")
                lines.append("| Measurement | Technology | # Assays |")
                lines.append("|-------------|-----------|----------|")
                for key, count in sorted(tech_summary.items()):
                    parts = key.split(" – ", 1)
                    lines.append(f"| {parts[0]} | {parts[1]} | {count} |")

                lines.append("")

                # Build the assay-details table as a separate string so we can wrap
                # it in a canonical block and write a CSV side-channel.
                assay_table_lines = [
                    "### Assay Details\n",
                    "| Assay ID | Technology | Measurement | Analysis Method | Factors 1 | Factors 2 | Material 1 | Material 2 |",
                    "|----------|-----------|-------------|-----------------|-----------|-----------|------------|------------|",
                ]
                csv_rows = []
                for a in assays:
                    f1 = ",".join(a.get('factors_1', [])) or 'N/A'
                    f2 = ",".join(a.get('factors_2', [])) or 'N/A'
                    assay_table_lines.append(
                        f"| `{a.get('assay_id', 'N/A')}` | {a.get('technology', 'N/A')} | "
                        f"{a.get('measurement', 'N/A')} | {a.get('analysis_method', 'N/A')} | "
                        f"{f1} | {f2} | {a.get('material_1', 'N/A')} | {a.get('material_2', 'N/A')} |"
                    )
                    csv_rows.append({
                        "assay_id": a.get('assay_id'),
                        "technology": a.get('technology'),
                        "measurement": a.get('measurement'),
                        "analysis_method": a.get('analysis_method'),
                        "factors_1": f1,
                        "factors_2": f2,
                        "material_1": a.get('material_1'),
                        "material_2": a.get('material_2'),
                    })
                assay_table_md = "\n".join(assay_table_lines)
                csv_path, csv_link = _write_results_csv(
                    csv_rows, f"study_{study_id}_assays"
                )
                csv_text = _render_csv_text(csv_rows)
                lines.append(_canonical_block(
                    assay_table_md, csv_path, csv_link,
                    context_label=f"assays for {study_id}",
                    csv_text=csv_text,
                ))
            else:
                lines.append("*No assays found for this study.*")

            return [
                types.TextContent(type="text", text="\n".join(lines), mimeType="text/markdown"),
            ]
        
        except Exception as e:
            logger.error(f"Error in get_study_info: {e}")
            return [types.TextContent(type="text", text=f"Error in get_study_info: {e}")]

    @mcp.tool(
        annotations={
            "title": "Select Assays (factor pair → assay IDs)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def select_assays(
        study_id: Optional[str] = None,
        selection: Optional[str] = None
    ) -> list[types.Content]:
        """USE THIS TOOL (not the `query` tool) to resolve a factor-pair condition
        (e.g. "Space Flight,Carcass vs Ground Control,Carcass") into the
        matching assay identifier(s) for a study. Falling back to `query` for
        this category loses the numbered factor menu, the per-match
        technology/measurement/method columns, and the next-step suggestions
        this tool emits.

        List and select assays for a study and render the response in markdown format.

        First call (selection=None):
        - If study_id missing, prompt for one (e.g., 'OSD-253').
        - Build a list of unique factor arrays across all assays.
        - Return a numbered menu as a markdown table.

        Second call (selection='i,j,k,l,...,m,n'):
        - Pairs consecutive indices: (i,j), (k,l), ..., (m,n)
        - Must provide an even number of indices
        - For each pair, returns the matching assay(s) with technology,
          measurement, differential analysis method, and material columns
          so the assistant can pick the right assay for a downstream
          question (e.g., RNA-Seq for DE genes, WGBS for DM regions,
          amplicon for organism abundance) WITHOUT issuing a follow-up
          Cypher query.
        """
        if not study_id:
            return [types.TextContent(type="text", text="Please provide a study_id (e.g., OSD-253).")]

        cypher = """
        MATCH (s:Study {identifier: $study_id})-[:PERFORMED_SpAS]->(a:Assay)
        RETURN a.identifier                    AS assay_id,
               coalesce(a.factors_1, [])       AS f1,
               coalesce(a.factors_2, [])       AS f2,
               a.technology                    AS technology,
               a.measurement                   AS measurement,
               a.differential_analysis_method  AS method,
               a.material_name_1               AS material_1,
               a.material_name_2               AS material_2
        ORDER BY assay_id
        """

        try:
            async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                rows_json_str = await session.execute_read(_read, cypher, {"study_id": study_id})
            rows = json.loads(rows_json_str)
        except Exception as e:
            logger.exception("select_assays query failed")
            return [types.TextContent(type="text", text=f"Error querying study '{study_id}': {e}")]

        if not rows:
            return [types.TextContent(type="text", text=f"No assays found for study '{study_id}'.")]

        def normalize(arr):
            if not isinstance(arr, list):
                return []
            return [str(x) for x in arr if x is not None]

        unique_arrays = []
        seen = set()
        for r in rows:
            f1 = normalize(r.get("f1"))
            f2 = normalize(r.get("f2"))
            for arr in (f1, f2):
                if not arr:
                    continue
                key = tuple(arr)
                if key not in seen:
                    seen.add(key)
                    unique_arrays.append(arr)

        unique_arrays = sorted(unique_arrays)

        if not unique_arrays:
            return [types.TextContent(type="text", text=f"Study '{study_id}' has no non-empty factor arrays in any assay.")]

        def _fmt(arr):
            arr_str = ",".join(json.dumps(x) for x in arr)
            return arr_str.replace('"', '')


        if selection is None:
            lines = []
            lines.append(f"## Factor arrays across all assays for study: {study_id}")
            lines.append("")  # Empty line for better markdown spacing
            lines.append("**Choose an EVEN number of indices for pairwise comparisons, e.g., '1,2,3,4' creates pairs (1 vs 2) and (3 vs 4):**")
            lines.append("")  # Empty line before table
            # Create markdown table with index and factors array
            lines.append("| Index | Factors |")
            lines.append("|-------|---------|")
            for i, arr in enumerate(unique_arrays, 1):
                lines.append(f"| {i} | {_fmt(arr)} |")

            return [
                types.TextContent(type="text", text="\n".join(lines), mimeType="text/markdown"),
            ]

        parts = [p for p in re.split(r"[,\s]+", selection.strip()) if p]
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            return [types.TextContent(type="text", text=f"Please provide at least two indices like '1,2' or '1,2,3,4'. Got: '{selection}'.")]

        # Check for even number of indices
        if len(parts) % 2 != 0:
            return [types.TextContent(type="text", text=f"Please provide an EVEN number of indices for pairwise comparisons. Got {len(parts)} indices: {selection}.")]

        indices = [int(p) for p in parts]
        n = len(unique_arrays)
        
        # Validate all indices are in range and unique
        if not all(1 <= idx <= n for idx in indices):
            return [types.TextContent(type="text", text=f"All indices must be in range 1..{n}. Got: {indices}.")]

        # Create pairs from consecutive indices: (i,j), (k,l), ..., (m,n)
        pairs = [(indices[i], indices[i+1]) for i in range(0, len(indices), 2)]
        
        # Find assay IDs for each pair comparison
        comparisons = []
        for pair_idx, (idx1, idx2) in enumerate(pairs, 1):
            array1 = unique_arrays[idx1 - 1]
            array2 = unique_arrays[idx2 - 1]
            key1 = tuple(array1)
            key2 = tuple(array2)

            # Find matching assays. We collect the full row record so we can
            # surface technology / measurement / method per match — this is
            # what the assistant needs to pick the right assay for an
            # expression vs. methylation vs. abundance question without
            # having to issue a follow-up Cypher query.
            matches: list[dict] = []
            seen_ids: set = set()
            for r in rows:
                f1 = normalize(r.get("f1"))
                f2 = normalize(r.get("f2"))
                if (tuple(f1) == key1 and tuple(f2) == key2):
                    aid = r.get("assay_id")
                    if aid not in seen_ids:
                        seen_ids.add(aid)
                        matches.append({
                            "assay_id": aid,
                            "technology": r.get("technology"),
                            "measurement": r.get("measurement"),
                            "method": r.get("method"),
                            "material_1": r.get("material_1"),
                            "material_2": r.get("material_2"),
                        })

            comparisons.append({
                "pair_number": pair_idx,
                "index1": idx1,
                "array1": array1,
                "index2": idx2,
                "array2": array2,
                "matches": matches,
                "selected_assay_id": matches[0]["assay_id"] if len(matches) == 1 else None,
            })

        # Build response
        lines = []
        lines.append(f"## Selected Assays for {study_id}\n")

        for comp in comparisons:
            lines.append(f"### Pair {comp['pair_number']}: Index {comp['index1']} vs Index {comp['index2']}")
            lines.append(f"**Condition 1 (Index {comp['index1']}):** {_fmt(comp['array1'])}")
            lines.append(f"**Condition 2 (Index {comp['index2']}):** {_fmt(comp['array2'])}")

            if not comp['matches']:
                lines.append("**Status:** No matching assay found")
            elif len(comp['matches']) == 1:
                m = comp['matches'][0]
                lines.append(f"**Assay ID:** `{m['assay_id']}`")
                meta_bits = []
                if m.get('technology'):  meta_bits.append(f"technology: {m['technology']}")
                if m.get('measurement'): meta_bits.append(f"measurement: {m['measurement']}")
                if m.get('method'):      meta_bits.append(f"method: {m['method']}")
                if meta_bits:
                    lines.append("**Assay metadata:** " + "; ".join(meta_bits))
            else:
                # Multiple matches — render a markdown table so the
                # assistant (and the user) can pick the right assay by
                # technology/measurement without a follow-up Cypher query.
                lines.append(f"**Status:** {len(comp['matches'])} matching assays — pick by technology / measurement:\n")
                lines.append("| Assay ID | Technology | Measurement | Method | Material 1 | Material 2 |")
                lines.append("|----------|------------|-------------|--------|------------|------------|")
                for m in comp['matches']:
                    lines.append(
                        f"| `{m['assay_id']}` | "
                        f"{m.get('technology') or 'N/A'} | "
                        f"{m.get('measurement') or 'N/A'} | "
                        f"{m.get('method') or 'N/A'} | "
                        f"{m.get('material_1') or 'N/A'} | "
                        f"{m.get('material_2') or 'N/A'} |"
                    )
            lines.append("")
        
        # Add suggested next steps
        lines.append("\n## Suggested Next Steps:\n")
        
        if len(pairs) == 1:
            # Single pair - standard analysis
            lines.append("1. Find differentially expressed genes")
            lines.append("2. Create a volcano plot")
            lines.append("3. Map differentially expressed genes to pathways, gene and protein function, diseases, etc., using the `humanspoke` (human) KG or `spoke` KG (human + bacterial genes) MCP services.")
        else:
            # Multiple pairs - suggest comparative analysis
            lines.append("1. Find differentially expressed genes for each comparison")
            lines.append("2. Create volcano plots for individual comparisons")
            lines.append("3. Identify genes that show consistent changes across comparisons")
            lines.append("4. Map differentially expressed genes to pathways, gene and protein function, diseases, etc., using the `humanspoke` (human) KG or `spoke` KG (human + bacterial genes) MCP services")

        if len(pairs) < 4:
            lines.append("5. Create a venn diagram to show overlap of common differentially expressed genes")
        
        return [
            types.TextContent(type="text", text="\n".join(lines), mimeType="text/markdown"),
        ]
    
    @mcp.tool(
        annotations={
            "title": "Find Differentially Expressed Genes (single assay)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_differentially_expressed_genes(
        assay_id: str = Field(..., description="Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')"),
        top_n: Optional[int] = Field(
            10,
            description=(
                "How many genes to display for each of up- and down-regulated "
                "lists. Pass None (or omit) to return ALL genes passing the "
                "adj_p_value filter rather than a top-N — useful when the user "
                "asks for 'all significantly upregulated genes' or wants the "
                "complete filtered set as CSV. When the full set is large, the "
                "CSV contains every row passing the filter."
            ),
        ),
        adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold for significance (default: 0.05). Only genes with adj_p_value <= this value are returned.")
    ) -> list[types.TextContent]:
        """USE THIS TOOL (not the `query` tool) for any "up/down-regulated genes",
        "DEGs", or "differential expression" question for a single assay.

        The `query` tool can express the same Cypher, but using it for this
        category loses the formatted markdown tables, the inline CSV
        side-channel, the proper adj_p_value handling, and the parallel
        execution of the count queries this tool already provides correctly.

        Return the top-N up- and down-regulated genes for a given assay_id.

        This tool runs two queries on the GeneLab KG:
          1) Top-N upregulated genes (log2fc > 0, adj_p_value <= adj_p_threshold, highest log2fc first)
          2) Top-N downregulated genes (log2fc < 0, adj_p_value <= adj_p_threshold, lowest log2fc first)

        Returning all passing rows:
          Pass `top_n=None` to return ALL rows that pass the filters rather
          than a top-N ranking. Use this when the user asks for "all
          significantly up/downregulated genes" or wants the complete
          filtered set as CSV.

        Results include gene symbol, gene name, log2 fold change, adjusted p-value,
        and group means and standard deviations for each condition.

        """

        factors_cypher = """
        MATCH (a:Assay {identifier: $assay_id})
        RETURN a.factors_1 AS factors_1, a.factors_2 AS factors_2
        """

        # Conditionally include LIMIT only when top_n is a positive integer.
        # Neo4j does not accept LIMIT NULL, so when the caller wants ALL rows
        # we drop the clause entirely. This matches the find_differentially_methylated_regions
        # behavior and is what makes top_n=None produce the full filtered set
        # in one Cypher call.
        limit_clause = "\n        LIMIT $top_n" if top_n is not None else ""

        up_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})
              -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
              (mg:MGene)
        WHERE r.log2fc > 0 AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
        RETURN
          mg.symbol          AS gene_symbol,
          mg.name            AS gene_name,
          r.log2fc           AS log2fc,
          r.adj_p_value      AS adj_p_value,
          r.group_mean_1     AS group_mean_1,
          r.group_stdev_1    AS group_stdev_1,
          r.group_mean_2     AS group_mean_2,
          r.group_stdev_2    AS group_stdev_2
        ORDER BY r.log2fc DESC{limit_clause}
        """

        down_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})
              -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
              (mg:MGene)
        WHERE r.log2fc < 0 AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
        RETURN
          mg.symbol          AS gene_symbol,
          mg.name            AS gene_name,
          r.log2fc           AS log2fc,
          r.adj_p_value      AS adj_p_value,
          r.group_mean_1     AS group_mean_1,
          r.group_stdev_1    AS group_stdev_1,
          r.group_mean_2     AS group_mean_2,
          r.group_stdev_2    AS group_stdev_2
        ORDER BY r.log2fc ASC{limit_clause}
        """

        count_up_cypher = """
        MATCH (a:Assay {identifier: $assay_id})-[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(mg:MGene)
        WHERE r.log2fc > 0 AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
        RETURN count(*) AS total
        """

        count_down_cypher = """
        MATCH (a:Assay {identifier: $assay_id})-[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(mg:MGene)
        WHERE r.log2fc < 0 AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
        RETURN count(*) AS total
        """
    
        try:

            # Run the 5 independent reads concurrently across separate sessions.
            # Each query opens its own session (Neo4j sessions are not safe for
            # concurrent transactions); the driver's connection pool handles
            # multiplexing efficiently. Serially this was 5 RTTs; in parallel
            # it's 1 RTT-equivalent for the slowest query.
            async def _run(cypher, params):
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, cypher, params)

            factors_params = {"assay_id": assay_id}
            # top_n is only referenced by Cypher when limit_clause is non-empty.
            # Include it in params only when the query actually uses it; Neo4j
            # tolerates extra params but keeping the two paired makes the code
            # clearer and parallels the methylation tool's behavior.
            up_params = {"assay_id": assay_id, "adj_p_threshold": adj_p_threshold}
            if top_n is not None:
                up_params["top_n"] = top_n
            down_params = up_params  # identical
            count_params = {"assay_id": assay_id, "adj_p_threshold": adj_p_threshold}

            (factors_json_str, up_json_str, down_json_str,
             count_up_str, count_down_str) = await asyncio.gather(
                _run(factors_cypher, factors_params),
                _run(up_cypher, up_params),
                _run(down_cypher, down_params),
                _run(count_up_cypher, count_params),
                _run(count_down_cypher, count_params),
            )

            factors_data = json.loads(factors_json_str)
            up = json.loads(up_json_str)
            down = json.loads(down_json_str)
            count_up_parsed = json.loads(count_up_str)
            count_down_parsed = json.loads(count_down_str)
            total_up = count_up_parsed[0]['total'] if count_up_parsed else 0
            total_down = count_down_parsed[0]['total'] if count_down_parsed else 0

            if factors_data:
                f1 = factors_data[0].get('factors_1', [])
                f2 = factors_data[0].get('factors_2', [])
                group1_label = ",".join(f1) if f1 else "Group 1"
                group2_label = ",".join(f2) if f2 else "Group 2"
            else:
                group1_label = "Group 1"
                group2_label = "Group 2"
    
            def _fmt_de_table(rows, title, total_count):
                if not rows:
                    return f"## **{title}**\nNo significantly {title.lower()} genes were found.\n"
                lines = [
                    f"## **{title}** (showing {len(rows)} of {total_count} total)\n",
                    f"| Gene | Gene Name | Log2FC | Adj.p-value | Mean ({group1_label}) | SD ({group1_label}) | Mean ({group2_label}) | SD ({group2_label}) |",
                    "|------|-----------|--------|-------------|" + "------------|" * 4
                ]
                def _fmt(v, spec):
                    return format(v, spec) if v is not None else 'N/A'

                for r in rows:
                    sym = r.get('gene_symbol', 'N/A')
                    name = r.get('gene_name', 'N/A')
                    l2fc = _fmt(r.get('log2fc'), '.4f')
                    adj_p = r.get('adj_p_value') if r.get('adj_p_value') is not None else 'N/A'
                    gm1   = _fmt(r.get('group_mean_1'),  '.3f')
                    gs1   = _fmt(r.get('group_stdev_1'), '.3f')
                    gm2   = _fmt(r.get('group_mean_2'),  '.3f')
                    gs2   = _fmt(r.get('group_stdev_2'), '.3f')
                    lines.append(f"| **{sym}** | {name} | {l2fc} | {adj_p} | {gm1} | {gs1} | {gm2} | {gs2} |")
                return "\n".join(lines)
    
            human_lines = [
                f"Top differentially expressed genes for assay: `{assay_id}`\n",
            ]

            # Up table: write CSV and wrap in canonical block
            up_table_md = _fmt_de_table(up, "Upregulated Genes", total_up)
            up_csv_path, up_csv_link = _write_results_csv(
                up, f"de_genes_up_{assay_id}_top{top_n}_p{adj_p_threshold}"
            )
            up_csv_text = _render_csv_text(up)
            human_lines.append(_canonical_block(
                up_table_md, up_csv_path, up_csv_link,
                context_label="upregulated genes table",
                csv_text=up_csv_text,
            ))
            human_lines.append("")

            # Down table: write CSV and wrap in canonical block
            down_table_md = _fmt_de_table(down, "Downregulated Genes", total_down)
            down_csv_path, down_csv_link = _write_results_csv(
                down, f"de_genes_down_{assay_id}_top{top_n}_p{adj_p_threshold}"
            )
            down_csv_text = _render_csv_text(down)
            human_lines.append(_canonical_block(
                down_table_md, down_csv_path, down_csv_link,
                context_label="downregulated genes table",
                csv_text=down_csv_text,
            ))

            return [
                types.TextContent(type="text", text="\n".join(human_lines), mimeType="text/markdown"),
            ]
    
        except Exception as e:
            logger.error(f"Error in find_differentially_expressed_genes: {e}")
            return [types.TextContent(type="text", text=f"Error in find_differentially_expressed_genes: {e}")]

    @mcp.tool(
        annotations={
            "title": "Find Differentially Methylated Regions (pool-aware)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_differentially_methylated_regions(
        assay_id: "str | list[str]" = Field(
            ...,
            description=(
                "Assay identifier, or a list of assay identifiers to pool. "
                "Pass a single string (e.g. 'OSD-48-968c...') to query one "
                "assay. Pass a list (e.g. ['OSD-48-968c...', 'OSD-48-4dc1...']) "
                "to pool results across multiple methylation assays — useful "
                "when a study has independent replicate methylation assays "
                "with identical factor spaces and you want the combined "
                "evidence. The output table will have an additional "
                "'assay_id' column so each row's source is identifiable, "
                "and the same region significantly methylated in two pooled "
                "assays will appear as two rows."
            ),
        ),
        top_n: Optional[int] = Field(
            10,
            description=(
                "How many regions to display for each of hyper- and hypo-methylated "
                "lists. Pass None (or omit) to return ALL rows passing the filters "
                "rather than a top-N — useful when the user asks for 'all "
                "hypermethylated promoter regions' or wants to download the "
                "complete filtered set as CSV. When the full set is large "
                "(>100 rows), the inline markdown table is capped at the "
                "first 100 rows for readability while the CSV contains every "
                "row."
            ),
        ),
        q_value_threshold: float = Field(0.05, description="q-value threshold for significance (default: 0.05). Only regions with q_value <= this value are returned."),
        methylation_diff_threshold: float = Field(
            0.0,
            description=(
                "Minimum |methylation_diff| magnitude (in percentage points) "
                "required for a row to be kept (default: 0.0 = any change). "
                "Hypermethylated rows must have methylation_diff > "
                "methylation_diff_threshold; hypomethylated rows must have "
                "methylation_diff < -methylation_diff_threshold. The "
                "GeneLab KG stores methylation_diff as a raw percentage, so "
                "a threshold of 10 means '10 percentage points or more', "
                "matching typical methylKit reporting conventions."
            ),
        ),
        in_promoter: Optional[bool] = Field(
            None,
            description=(
                "MethylationRegion location filter. When True, return only regions "
                "overlapping a gene promoter (mr.in_promoter = true). When False, "
                "exclude promoter-overlapping regions (mr.in_promoter = false). None "
                "(default) imposes no promoter filter. The filter is applied during "
                "the Cypher query, so 'top_n' and the 'showing N of TOTAL' count "
                "both reflect the filtered set — promoter-only top-N rankings are "
                "computed from the promoter-only universe, NOT post-filtered from "
                "the global top-N."
            ),
        ),
        in_exon: Optional[bool] = Field(
            None,
            description=(
                "MethylationRegion location filter. When True, return only regions "
                "overlapping an exon (mr.in_exon = true). When False, exclude "
                "exon-overlapping regions. None (default) imposes no exon filter. "
                "Combines with other filters via AND."
            ),
        ),
        in_intron: Optional[bool] = Field(
            None,
            description=(
                "MethylationRegion location filter. When True, return only regions "
                "overlapping an intron (mr.in_intron = true). When False, exclude "
                "intron-overlapping regions. None (default) imposes no intron "
                "filter. Combines with other filters via AND."
            ),
        ),
        dist_to_feature_max: Optional[int] = Field(
            None,
            description=(
                "MethylationRegion distance filter. When set, return only regions "
                "whose dist_to_feature (bp to the nearest annotated gene feature) "
                "is <= this value. None (default) imposes no distance filter. "
                "Useful for narrowing to regions tightly associated with gene bodies."
            ),
        ),
    ) -> list[types.TextContent]:
        """USE THIS TOOL (not the `query` tool) for any "hyper/hypo-methylated
        regions", "DMRs", or "differential methylation" question — including
        all-rows requests, promoter/exon/intron-filtered methylation queries,
        and pooling methylation across replicate assays. Falling back to `query`
        for this category reproduces the work this tool already does (proper
        IS-NOT-NULL guards, gene-symbol join, parallel count queries, location
        filtering, deterministic CSV emission) and loses the formatted markdown
        + inline CSV side-channel.

        Return hyper- and hypo-methylated regions for one or more assays.

        This tool runs two queries on the GeneLab KG:
          1) Hypermethylated regions (methylation_diff > methylation_diff_threshold,
             q_value <= q_value_threshold, highest first)
          2) Hypomethylated regions  (methylation_diff < -methylation_diff_threshold,
             q_value <= q_value_threshold, lowest first)

        Pooling across assays:
          Pass a list to `assay_id` to pool results across multiple methylation
          assays (the Cypher uses `a.identifier IN $assay_ids`). Common use case:
          a study has 2+ methylation assays representing replicate runs of the
          same comparison, and the user wants the combined evidence rather than
          per-assay tables. The output adds an `assay_id` column so each row's
          source is visible.

        Returning all passing rows:
          Pass `top_n=None` to return ALL rows that pass the filters rather
          than a top-N ranking. Use this when the user asks for "all
          hypermethylated promoter regions" or wants the complete filtered
          set as CSV. To keep the response readable when the full set is
          large, the inline markdown table is capped at the first 100 rows
          and a note is shown; the CSV contains every row.

        MethylationRegion location filters (in_promoter, in_exon, in_intron,
        dist_to_feature_max) are applied DURING the Cypher query. This means
        when a user asks for "top hypermethylated regions in the promoter",
        pass in_promoter=True and the tool will correctly rank within the
        promoter universe. Do NOT post-filter the unfiltered top-N — that
        produces wrong results because regions outside the filter (e.g.
        non-promoter regions) occupy slots in the unfiltered top-N and
        displace correctly-ranked filtered regions. Multiple filters combine
        with AND, so in_promoter=True with in_intron=False returns promoter
        regions that are not intronic.

        WHEN TO USE THIS TOOL vs. direct Cypher via the `query` tool:
        Use THIS tool for any "hyper/hypo methylated regions" question,
        including pooled-across-assays, all-passing-rows, and filter-by-location
        variants. It produces formatted markdown tables AND an inline CSV
        that the user can save directly. Falling back to direct Cypher loses
        the formatted output and the CSV.
        """

        # Normalize assay_id to a list so the pooled and single-assay paths
        # share a single implementation. The Cypher always uses
        # `WHERE a.identifier IN $assay_ids` regardless of how many were passed.
        if isinstance(assay_id, str):
            assay_ids = [assay_id]
        else:
            assay_ids = list(assay_id)
        if not assay_ids:
            return [types.TextContent(
                type="text",
                text="Error: at least one assay_id must be provided.",
            )]

        # Build the MethylationRegion filter block. This is appended to every
        # query's WHERE clause (top-N + count) so the "showing N of TOTAL"
        # message reflects the filtered universe, not the global one. The
        # parameter values are passed through the query params dict; only
        # filters that are not None contribute clauses.
        mr_filter_parts: list[str] = []
        mr_filter_params: dict[str, Any] = {}
        if in_promoter is not None:
            mr_filter_parts.append("AND mr.in_promoter = $mr_in_promoter")
            mr_filter_params["mr_in_promoter"] = in_promoter
        if in_exon is not None:
            mr_filter_parts.append("AND mr.in_exon = $mr_in_exon")
            mr_filter_params["mr_in_exon"] = in_exon
        if in_intron is not None:
            mr_filter_parts.append("AND mr.in_intron = $mr_in_intron")
            mr_filter_params["mr_in_intron"] = in_intron
        if dist_to_feature_max is not None:
            mr_filter_parts.append("AND mr.dist_to_feature IS NOT NULL AND mr.dist_to_feature <= $mr_dist_max")
            mr_filter_params["mr_dist_max"] = dist_to_feature_max
        mr_filter_block = ("\n          " + "\n          ".join(mr_filter_parts)) if mr_filter_parts else ""

        # Conditionally include LIMIT only when top_n is a positive integer.
        # Neo4j does not accept LIMIT NULL, so when the caller wants ALL rows
        # we drop the clause entirely. This is what makes top_n=None produce
        # the full filtered set in one Cypher call.
        limit_clause = "\n        LIMIT $top_n" if top_n is not None else ""

        # The factors query picks the first assay's labels — pooled assays in
        # this tool's intended use case share the same factor space (which is
        # why pooling makes sense at all). If they differ, the labels still
        # come from a real assay in the set, which is better than an
        # invented composite label.
        factors_cypher = """
        MATCH (a:Assay {identifier: $first_assay_id})
        RETURN a.factors_1 AS factors_1, a.factors_2 AS factors_2
        """

        hyper_cypher = f"""
        MATCH (a:Assay)
              -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
              (mr:MethylationRegion)
              <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
        WHERE a.identifier IN $assay_ids
          AND r.methylation_diff > $methylation_diff_threshold
          AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{mr_filter_block}
        RETURN
          a.identifier         AS assay_id,
          mg.symbol            AS gene_symbol,
          mg.name              AS gene_name,
          mr.identifier        AS region,
          mr.in_promoter       AS in_promoter,
          r.methylation_diff   AS methylation_diff,
          r.q_value            AS q_value,
          r.group_mean_1       AS group_mean_1,
          r.group_stdev_1      AS group_stdev_1,
          r.group_mean_2       AS group_mean_2,
          r.group_stdev_2      AS group_stdev_2
        ORDER BY r.methylation_diff DESC{limit_clause}
        """

        hypo_cypher = f"""
        MATCH (a:Assay)
              -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
              (mr:MethylationRegion)
              <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
        WHERE a.identifier IN $assay_ids
          AND r.methylation_diff < -$methylation_diff_threshold
          AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{mr_filter_block}
        RETURN
          a.identifier         AS assay_id,
          mg.symbol            AS gene_symbol,
          mg.name              AS gene_name,
          mr.identifier        AS region,
          mr.in_promoter       AS in_promoter,
          r.methylation_diff   AS methylation_diff,
          r.q_value            AS q_value,
          r.group_mean_1       AS group_mean_1,
          r.group_stdev_1      AS group_stdev_1,
          r.group_mean_2       AS group_mean_2,
          r.group_stdev_2      AS group_stdev_2
        ORDER BY r.methylation_diff ASC{limit_clause}
        """

        count_hyper_cypher = f"""
        MATCH (a:Assay)-[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->(mr:MethylationRegion)<-[:METHYLATED_IN_MGmMR]-(mg:MGene)
        WHERE a.identifier IN $assay_ids
          AND r.methylation_diff > $methylation_diff_threshold
          AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{mr_filter_block}
        RETURN count(*) AS total
        """

        count_hypo_cypher = f"""
        MATCH (a:Assay)-[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->(mr:MethylationRegion)<-[:METHYLATED_IN_MGmMR]-(mg:MGene)
        WHERE a.identifier IN $assay_ids
          AND r.methylation_diff < -$methylation_diff_threshold
          AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{mr_filter_block}
        RETURN count(*) AS total
        """

        try:

            # Run the 5 independent reads concurrently (see find_differentially_expressed_genes
            # for rationale — same optimization, same connection-pool behavior).
            async def _run(cypher, params):
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, cypher, params)

            factors_params = {"first_assay_id": assay_ids[0]}
            # Shared params for the top-N row queries. top_n is only included
            # when the caller specified a limit; otherwise the LIMIT clause
            # was dropped and the parameter is unused.
            top_params: dict[str, Any] = {
                "assay_ids": assay_ids,
                "q_value_threshold": q_value_threshold,
                "methylation_diff_threshold": methylation_diff_threshold,
                **mr_filter_params,
            }
            if top_n is not None:
                top_params["top_n"] = top_n
            count_params = {
                "assay_ids": assay_ids,
                "q_value_threshold": q_value_threshold,
                "methylation_diff_threshold": methylation_diff_threshold,
                **mr_filter_params,
            }

            (factors_json_str, hyper_json_str, hypo_json_str,
             count_hyper_str, count_hypo_str) = await asyncio.gather(
                _run(factors_cypher, factors_params),
                _run(hyper_cypher, top_params),
                _run(hypo_cypher, top_params),
                _run(count_hyper_cypher, count_params),
                _run(count_hypo_cypher, count_params),
            )

            factors_data = json.loads(factors_json_str)
            hyper = json.loads(hyper_json_str)
            hypo = json.loads(hypo_json_str)
            count_hyper_parsed = json.loads(count_hyper_str)
            count_hypo_parsed = json.loads(count_hypo_str)
            total_hyper = count_hyper_parsed[0]['total'] if count_hyper_parsed else 0
            total_hypo = count_hypo_parsed[0]['total'] if count_hypo_parsed else 0

            if factors_data:
                f1 = factors_data[0].get('factors_1', [])
                f2 = factors_data[0].get('factors_2', [])
                group1_label = ",".join(f1) if f1 else "Group 1"
                group2_label = ",".join(f2) if f2 else "Group 2"
            else:
                group1_label = "Group 1"
                group2_label = "Group 2"

            # Display cap for the inline markdown table. The full CSV always
            # contains every returned row; this only affects what's rendered
            # as a markdown table for in-conversation reading. Past ~100 rows
            # the table becomes unreadable and just consumes context. Users
            # who need to inspect the full set use the CSV.
            MARKDOWN_TABLE_CAP = 100

            # When pooling multiple assays the assay_id column is the only
            # way to tell rows apart, so we include it. For single-assay
            # queries the column would just be the same value on every row —
            # we omit it to match the simpler GitHub-baseline display.
            show_assay_id_col = len(assay_ids) > 1

            def _fmt_meth_table(rows, title, total_count):
                if not rows:
                    return f"## **{title}**\nNo significantly {title.lower()} regions were found.\n"
                # Apply the markdown-display cap. The CSV (built separately
                # below) always uses the full row list.
                display_rows = rows[:MARKDOWN_TABLE_CAP]
                capped = len(rows) > MARKDOWN_TABLE_CAP

                header_cols = ["Gene", "Gene Name", "Region"]
                if show_assay_id_col:
                    header_cols.insert(0, "Assay")
                header_cols += [
                    "In Promoter", "Methylation Diff (%)", "q-value",
                    f"Mean ({group1_label})", f"SD ({group1_label})",
                    f"Mean ({group2_label})", f"SD ({group2_label})",
                ]
                header_line = "| " + " | ".join(header_cols) + " |"
                sep_line = "|" + "|".join(["------"] * len(header_cols)) + "|"

                if capped:
                    heading = (
                        f"## **{title}** (showing first {len(display_rows)} of "
                        f"{total_count} total — full set in CSV below)\n"
                    )
                else:
                    heading = f"## **{title}** (showing {len(display_rows)} of {total_count} total)\n"
                lines = [heading, header_line, sep_line]

                for r in display_rows:
                    gene = r.get('gene_symbol') or 'N/A'
                    name = r.get('gene_name') or 'N/A'
                    region = r.get('region', 'N/A')
                    aid = r.get('assay_id', 'N/A')
                    in_prom = r.get('in_promoter')
                    in_prom_str = str(in_prom) if in_prom is not None else 'N/A'
                    md = r.get('methylation_diff')
                    qv = r.get('q_value')
                    gm1 = r.get('group_mean_1'); gs1 = r.get('group_stdev_1')
                    gm2 = r.get('group_mean_2'); gs2 = r.get('group_stdev_2')
                    md_str  = f"{md:.3f}"  if md  is not None else 'N/A'
                    qv_str  = f"{qv}"      if qv  is not None else 'N/A'
                    gm1_str = f"{gm1:.3f}" if gm1 is not None else 'N/A'
                    gs1_str = f"{gs1:.3f}" if gs1 is not None else 'N/A'
                    gm2_str = f"{gm2:.3f}" if gm2 is not None else 'N/A'
                    gs2_str = f"{gs2:.3f}" if gs2 is not None else 'N/A'

                    cells = [f"**{gene}**", name, region]
                    if show_assay_id_col:
                        cells.insert(0, aid)
                    cells += [in_prom_str, md_str, qv_str, gm1_str, gs1_str, gm2_str, gs2_str]
                    lines.append("| " + " | ".join(cells) + " |")
                return "\n".join(lines)

            # Header line: name the assay(s) so the response is self-describing
            # even when pooled across multiple methylation assays.
            if len(assay_ids) == 1:
                header_line = f"Top differentially methylated regions for assay: `{assay_ids[0]}`\n"
            else:
                assay_list_md = ", ".join(f"`{a}`" for a in assay_ids)
                header_line = f"Top differentially methylated regions pooled across {len(assay_ids)} assays: {assay_list_md}\n"
            human_lines = [header_line]

            # Reflect any active MR filters and the threshold/pool state in the
            # suggested CSV filename so the saved file is self-describing — a
            # user who downloads both an unfiltered and a promoter-only run from
            # the same assay gets two distinct filenames, and pooled runs are
            # clearly labeled as such.
            filter_tag_parts: list[str] = []
            if in_promoter is True:
                filter_tag_parts.append("promoter")
            elif in_promoter is False:
                filter_tag_parts.append("nopromoter")
            if in_exon is True:
                filter_tag_parts.append("exon")
            elif in_exon is False:
                filter_tag_parts.append("noexon")
            if in_intron is True:
                filter_tag_parts.append("intron")
            elif in_intron is False:
                filter_tag_parts.append("nointron")
            if dist_to_feature_max is not None:
                filter_tag_parts.append(f"dist{dist_to_feature_max}")
            if methylation_diff_threshold != 0.0:
                filter_tag_parts.append(f"md{methylation_diff_threshold}")
            filter_tag = ("_" + "_".join(filter_tag_parts)) if filter_tag_parts else ""

            # Filename stem: use the single assay_id for non-pooled queries to
            # match the prior naming; use a pooled tag for multi-assay queries.
            if len(assay_ids) == 1:
                stem_id = assay_ids[0]
            else:
                stem_id = f"pooled{len(assay_ids)}assays_{assay_ids[0]}"
            top_n_tag = f"top{top_n}" if top_n is not None else "all"

            hyper_table_md = _fmt_meth_table(hyper, "Hypermethylated Regions", total_hyper)
            hyper_csv_path, hyper_csv_link = _write_results_csv(
                hyper, f"dm_regions_hyper_{stem_id}_{top_n_tag}_q{q_value_threshold}{filter_tag}"
            )
            hyper_csv_text = _render_csv_text(hyper)
            human_lines.append(_canonical_block(
                hyper_table_md, hyper_csv_path, hyper_csv_link,
                context_label="hypermethylated regions table",
                csv_text=hyper_csv_text,
            ))
            human_lines.append("")

            hypo_table_md = _fmt_meth_table(hypo, "Hypomethylated Regions", total_hypo)
            hypo_csv_path, hypo_csv_link = _write_results_csv(
                hypo, f"dm_regions_hypo_{stem_id}_{top_n_tag}_q{q_value_threshold}{filter_tag}"
            )
            hypo_csv_text = _render_csv_text(hypo)
            human_lines.append(_canonical_block(
                hypo_table_md, hypo_csv_path, hypo_csv_link,
                context_label="hypomethylated regions table",
                csv_text=hypo_csv_text,
            ))

            return [
                types.TextContent(type="text", text="\n".join(human_lines), mimeType="text/markdown"),
            ]

        except Exception as e:
            logger.error(f"Error in find_differentially_methylated_regions: {e}")
            return [types.TextContent(type="text", text=f"Error in find_differentially_methylated_regions: {e}")]

    @mcp.tool(
        annotations={
            "title": "Find Differentially Abundant Organisms (DESeq2/ANCOM-BC)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_differentially_abundant_organisms(
        assay_id: str = Field(..., description="Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')"),
        top_n: Optional[int] = Field(
            10,
            description=(
                "How many organisms to display for each of increased and "
                "decreased abundance lists. Pass None (or omit) to return ALL "
                "organisms passing the magnitude + significance filters rather "
                "than a top-N — useful when the user asks for 'all "
                "significantly increased organisms' or wants the complete "
                "filtered set as CSV. When the full set is large, the CSV "
                "contains every row passing the filter."
            ),
        ),
        adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold for DESeq2 abundance assays (default: 0.05). Applied to rows with adj_p_value populated."),
        q_value_threshold: float = Field(0.05, description="q-value threshold for ANCOM-BC abundance assays (default: 0.05). Applied to rows with q_value populated."),
        log2fc_threshold: float = Field(0.0, description="Minimum |log2fc| magnitude required for a row to be kept (default: 0.0 = any change). Applies to BOTH DESeq2 and ANCOM-BC rows since log2fc is populated for both methods."),
        lnfc_threshold: Optional[float] = Field(None, description="Optional minimum |lnfc| magnitude. Only applied to rows with lnfc populated (ANCOM-BC). DESeq2 rows are not filtered by this parameter. Leave unset (None) to skip lnfc filtering entirely.")
    ) -> list[types.TextContent]:
        """USE THIS TOOL (not the `query` tool) for any "differentially abundant
        organisms", DESeq2/ANCOM-BC abundance, or "increased/decreased
        abundance" question for a single assay.

        The `query` tool can express the same Cypher, but using it for this
        category loses the formatted markdown tables, the inline CSV
        side-channel, the method-aware significance filtering (DESeq2
        adj_p_value vs. ANCOM-BC q_value), and the safe lnfc handling that
        avoids silently dropping DESeq2 rows (where lnfc IS NULL would fail a
        naive `lnfc > 0` filter).

        Return the top-N organisms with increased and decreased differential abundance for a given assay_id.

        Direction (increase vs. decrease) and ranking use log2fc, which is populated for
        BOTH DESeq2 and ANCOM-BC abundance edges in the GeneLab KG. log2fc is therefore
        used as the universal direction/ordering field and as the default magnitude filter.

        The lnfc property is only populated for ANCOM-BC edges (DESeq2 leaves it null).
        It is returned in the result for reference and can be used as an additional
        magnitude filter via the lnfc_threshold parameter. log2fc is the direction
        filter because it is populated for both DESeq2 and ANCOM-BC rows; using lnfc
        for direction would null-filter every DESeq2 row out of the result.

        Significance filtering is method-aware:
          - DESeq2 edges populate adj_p_value (not q_value); the adj_p_threshold applies.
          - ANCOM-BC edges populate q_value (not adj_p_value); the q_value_threshold applies.
        Each row is kept if its populated significance field meets the matching threshold.

        Magnitude filtering:
          - log2fc_threshold (default 0.0) applies to every row, since log2fc is populated for both methods.
          - lnfc_threshold (default None = no filter) is method-aware: when set, it requires
            |lnfc| >= lnfc_threshold on rows where lnfc is populated. Rows where lnfc is null
            (i.e., DESeq2 rows) are not affected by this filter. Set lnfc_threshold to a
            positive number to add an ANCOM-BC-specific magnitude filter on top of log2fc.

        This tool runs two queries on the GeneLab KG:
          1) Top-N organisms with increased abundance (log2fc > log2fc_threshold, significance and optional lnfc filters met, highest log2fc first)
          2) Top-N organisms with decreased abundance (log2fc < -log2fc_threshold, significance and optional lnfc filters met, lowest log2fc first)

        Results include organism name, log2fc, lnfc, q-value, adj-p-value,
        and group means and standard deviations.
        """

        factors_cypher = """
        MATCH (a:Assay {identifier: $assay_id})
        RETURN a.factors_1 AS factors_1, a.factors_2 AS factors_2
        """

        # The lnfc clause is method-aware: when lnfc_threshold is None we omit it entirely
        # (no filter), when it is set we require |lnfc| >= threshold on rows where lnfc is
        # populated, and we leave rows with lnfc IS NULL untouched (DESeq2 rows). This
        # avoids the original bug where null comparisons silently dropped rows.
        if lnfc_threshold is None:
            lnfc_clause = ""
        else:
            lnfc_clause = (
                "          AND (r.lnfc IS NULL OR abs(r.lnfc) >= $lnfc_threshold)\n"
            )

        # Conditionally include LIMIT only when top_n is a positive integer.
        # Neo4j does not accept LIMIT NULL, so when the caller wants ALL rows
        # we drop the clause entirely. This matches the methylation tool's
        # behavior and is what makes top_n=None produce the full filtered set
        # in one Cypher call.
        limit_clause = "\n        LIMIT $top_n" if top_n is not None else ""

        up_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})
              -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
              (o:Organism)
        WHERE r.log2fc IS NOT NULL AND r.log2fc > $log2fc_threshold
          AND (
            (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
            OR (r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold)
          )
{lnfc_clause}        RETURN
          o.name             AS organism_name,
          o.identifier       AS organism_id,
          r.log2fc           AS log2fc,
          r.lnfc             AS lnfc,
          r.q_value          AS q_value,
          r.adj_p_value      AS adj_p_value,
          r.group_mean_1     AS group_mean_1,
          r.group_stdev_1    AS group_stdev_1,
          r.group_mean_2     AS group_mean_2,
          r.group_stdev_2    AS group_stdev_2
        ORDER BY r.log2fc DESC{limit_clause}
        """

        down_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})
              -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
              (o:Organism)
        WHERE r.log2fc IS NOT NULL AND r.log2fc < -$log2fc_threshold
          AND (
            (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
            OR (r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold)
          )
{lnfc_clause}        RETURN
          o.name             AS organism_name,
          o.identifier       AS organism_id,
          r.log2fc           AS log2fc,
          r.lnfc             AS lnfc,
          r.q_value          AS q_value,
          r.adj_p_value      AS adj_p_value,
          r.group_mean_1     AS group_mean_1,
          r.group_stdev_1    AS group_stdev_1,
          r.group_mean_2     AS group_mean_2,
          r.group_stdev_2    AS group_stdev_2
        ORDER BY r.log2fc ASC{limit_clause}
        """

        count_up_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})-[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->(o:Organism)
        WHERE r.log2fc IS NOT NULL AND r.log2fc > $log2fc_threshold
          AND (
            (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
            OR (r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold)
          )
{lnfc_clause}        RETURN count(*) AS total
        """

        count_down_cypher = f"""
        MATCH (a:Assay {{identifier: $assay_id}})-[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->(o:Organism)
        WHERE r.log2fc IS NOT NULL AND r.log2fc < -$log2fc_threshold
          AND (
            (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
            OR (r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold)
          )
{lnfc_clause}        RETURN count(*) AS total
        """
    
        try:

            # Build the parameter dict shared across all four queries. lnfc_threshold is
            # only included when the user supplied it; the query string itself was built
            # to omit the $lnfc_threshold reference when lnfc_threshold is None, so the
            # parameter would otherwise be unused — Neo4j tolerates extra params, but
            # keeping them paired makes the code clearer.
            base_params = {
                "assay_id": assay_id,
                "adj_p_threshold": adj_p_threshold,
                "q_value_threshold": q_value_threshold,
                "log2fc_threshold": log2fc_threshold,
            }
            if lnfc_threshold is not None:
                base_params["lnfc_threshold"] = lnfc_threshold

            # Run the 5 independent reads concurrently (see find_differentially_expressed_genes
            # for rationale).
            async def _run(cypher, params):
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, cypher, params)

            # top_n is only referenced when limit_clause is non-empty. Including it
            # in params when LIMIT was dropped is harmless (Neo4j ignores unused
            # params), but constructing it conditionally documents the contract.
            row_params = dict(base_params)
            if top_n is not None:
                row_params["top_n"] = top_n

            (factors_json_str, up_json_str, down_json_str,
             count_up_str, count_down_str) = await asyncio.gather(
                _run(factors_cypher, {"assay_id": assay_id}),
                _run(up_cypher, row_params),
                _run(down_cypher, row_params),
                _run(count_up_cypher, base_params),
                _run(count_down_cypher, base_params),
            )

            factors_data = json.loads(factors_json_str)
            up = json.loads(up_json_str)
            down = json.loads(down_json_str)
            count_up_parsed = json.loads(count_up_str)
            count_down_parsed = json.loads(count_down_str)
            total_up = count_up_parsed[0]['total'] if count_up_parsed else 0
            total_down = count_down_parsed[0]['total'] if count_down_parsed else 0

            if factors_data:
                f1 = factors_data[0].get('factors_1', [])
                f2 = factors_data[0].get('factors_2', [])
                group1_label = ",".join(f1) if f1 else "Group 1"
                group2_label = ",".join(f2) if f2 else "Group 2"
            else:
                group1_label = "Group 1"
                group2_label = "Group 2"
    
            def _fmt_abund_table(rows, title, total_count):
                if not rows:
                    return f"## **{title}**\nNo significantly {title.lower()} organisms were found.\n"
                # Show both adj_p_value (DESeq2) and q_value (ANCOM-BC) columns; for any given
                # row exactly one will be populated and the other will read N/A. This makes the
                # underlying analysis method visible without having to look it up separately.
                lines = [
                    f"## **{title}** (showing {len(rows)} of {total_count} total)\n",
                    f"| Organism | Log2FC | LnFC | adj.p (DESeq2) | q-value (ANCOM-BC) | Mean ({group1_label}) | SD ({group1_label}) | Mean ({group2_label}) | SD ({group2_label}) |",
                    "|----------|--------|------|----------------|--------------------|" + "------------|" * 4
                ]
                def _fmt(v, spec):
                    return format(v, spec) if v is not None else 'N/A'

                for r in rows:
                    org   = r.get('organism_name', 'N/A')
                    l2fc  = _fmt(r.get('log2fc'),       '.4f')
                    lnfc  = _fmt(r.get('lnfc'),         '.4f')
                    adj_p = r.get('adj_p_value') if r.get('adj_p_value') is not None else 'N/A'
                    qv    = r.get('q_value')     if r.get('q_value')     is not None else 'N/A'
                    gm1   = _fmt(r.get('group_mean_1'),  '.3f')
                    gs1   = _fmt(r.get('group_stdev_1'), '.3f')
                    gm2   = _fmt(r.get('group_mean_2'),  '.3f')
                    gs2   = _fmt(r.get('group_stdev_2'), '.3f')
                    lines.append(f"| **{org}** | {l2fc} | {lnfc} | {adj_p} | {qv} | {gm1} | {gs1} | {gm2} | {gs2} |")
                return "\n".join(lines)

            human_lines = [
                f"Top differentially abundant organisms for assay: `{assay_id}`\n",
            ]

            up_table_md = _fmt_abund_table(up, "Increased Abundance", total_up)
            up_csv_path, up_csv_link = _write_results_csv(
                up, f"da_orgs_up_{assay_id}_top{top_n}_p{adj_p_threshold}_q{q_value_threshold}"
            )
            up_csv_text = _render_csv_text(up)
            human_lines.append(up_table_md)
            if up_csv_path:
                human_lines.append(f"\n_CSV: `{up_csv_path}`_")
            human_lines.append("")

            down_table_md = _fmt_abund_table(down, "Decreased Abundance", total_down)
            down_csv_path, down_csv_link = _write_results_csv(
                down, f"da_orgs_down_{assay_id}_top{top_n}_p{adj_p_threshold}_q{q_value_threshold}"
            )
            down_csv_text = _render_csv_text(down)
            human_lines.append(down_table_md)
            if down_csv_path:
                human_lines.append(f"\n_CSV: `{down_csv_path}`_")

            return [
                types.TextContent(type="text", text="\n".join(human_lines), mimeType="text/markdown"),
            ]
    
        except Exception as e:
            logger.error(f"Error in find_differentially_abundant_organisms: {e}")
            return [types.TextContent(type="text", text=f"Error in find_differentially_abundant_organisms: {e}")]

    @mcp.tool(
        annotations={
            "title": "Find Common Differentially Expressed Genes (across assays)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_common_differentially_expressed_genes(
            assay_ids: list[str] = Field(..., description="List of assay identifiers (e.g., ['OSD-253-abc123', 'OSD-253-def456'])"),
            log2fc_threshold: float = Field(1.0, description="Log2 fold change threshold for filtering genes (default: 1.0 = 2-fold change)"),
            adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold for significance (default: 0.05, max value: 0.1)")
        ) -> list[types.TextContent]:
            """USE THIS TOOL (not the `query` tool) for any "common DEGs across
            assays", "genes upregulated in BOTH assays", "shared
            up/downregulated genes", or any cross-assay DE intersection
            question. Falling back to `query` for this category reproduces the
            per-assay fetch + inner-join logic this tool already does correctly
            (parallel fetches via the driver's connection pool, |log2fc| >
            threshold filtering, separate up vs. down intersections) and loses
            the formatted markdown tables, the inline CSV side-channel, and
            the assay-reference legend.

            Find common differentially expressed genes across multiple assays.
            
            This function:
            1. Takes a list of assay IDs as input (2 or more)
            2. Gets ALL genes with |log2fc| > threshold for each assay
            3. Inner joins among the upregulated genes and among the downregulated genes
            4. Returns a markdown table with columns: gene, assay_1, assay_2, ..., assay_n showing log2fc values
            
            """
            
            if len(assay_ids) < 2:
                return [types.TextContent(
                    type="text", 
                    text="Error: Please provide at least 2 assay IDs to find correlated genes."
                )]
            
            try:
                # Step 1: Get differentially expressed genes for each assay
                upregulated_genes = {}  # {assay_id: {gene_symbol: log2fc}}
                downregulated_genes = {}  # {assay_id: {gene_symbol: log2fc}}

                # Query for upregulated genes - NO LIMIT, uses threshold
                up_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[m:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (g:MGene)
                WHERE m.log2fc > $log2fc_threshold
                  AND m.adj_p_value IS NOT NULL AND m.adj_p_value <= $adj_p_threshold
                RETURN g.symbol AS gene_symbol, m.log2fc AS log2fc
                ORDER BY m.log2fc DESC
                """

                # Query for downregulated genes - NO LIMIT, uses threshold
                down_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[m:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (g:MGene)
                WHERE m.log2fc < -$log2fc_threshold
                  AND m.adj_p_value IS NOT NULL AND m.adj_p_value <= $adj_p_threshold
                RETURN g.symbol AS gene_symbol, m.log2fc AS log2fc
                ORDER BY m.log2fc ASC
                """

                # Fetch up/down gene sets for every assay concurrently. Each
                # query opens its own session because Neo4j sessions aren't safe
                # for concurrent transactions; running them in parallel across
                # the driver's connection pool collapses 2N sequential RTTs to
                # ~1 RTT-equivalent.
                async def _fetch(aid, cypher):
                    params = {
                        "assay_id": aid,
                        "log2fc_threshold": log2fc_threshold,
                        "adj_p_threshold": adj_p_threshold,
                    }
                    async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                        result_json = await s.execute_read(_read, cypher, params)
                    return json.loads(result_json)

                # Build the task list: up and down for each assay
                up_tasks = [_fetch(aid, up_query) for aid in assay_ids]
                down_tasks = [_fetch(aid, down_query) for aid in assay_ids]
                up_results, down_results = await asyncio.gather(
                    asyncio.gather(*up_tasks),
                    asyncio.gather(*down_tasks),
                )

                for aid, up_data in zip(assay_ids, up_results):
                    upregulated_genes[aid] = {row['gene_symbol']: row['log2fc'] for row in up_data}
                for aid, down_data in zip(assay_ids, down_results):
                    downregulated_genes[aid] = {row['gene_symbol']: row['log2fc'] for row in down_data}
                
                # Step 2: Find common upregulated genes (inner join)
                common_up_genes = set(upregulated_genes[assay_ids[0]].keys())
                for assay_id in assay_ids[1:]:
                    common_up_genes &= set(upregulated_genes[assay_id].keys())
                
                # Step 3: Find common downregulated genes (inner join)
                common_down_genes = set(downregulated_genes[assay_ids[0]].keys())
                for assay_id in assay_ids[1:]:
                    common_down_genes &= set(downregulated_genes[assay_id].keys())
                
                # Step 4: Build markdown tables
                markdown_output = f"## Common Differentially Expressed Genes\n\n"
                markdown_output += f"**Log2FC Threshold:** ±{log2fc_threshold} (≥{2**log2fc_threshold:.1f}-fold change)"
                markdown_output += f"**Adjusted p-value Threshold:** {adj_p_threshold}\n\n"

                # Helper: build CSV-ready rows from a set of common genes + per-assay log2fc dicts
                def _build_common_rows(common_genes, per_assay_dict):
                    rows = []
                    for gene in sorted(common_genes):
                        row = {"gene_symbol": gene}
                        for i, aid in enumerate(assay_ids):
                            row[f"assay_{i+1}_log2fc"] = per_assay_dict[aid][gene]
                        rows.append(row)
                    return rows

                # Helper: build the markdown table body (header + rows) for the canonical block
                def _build_common_table_md(title, common_genes, per_assay_dict, total_label):
                    if not common_genes:
                        return f"### {title}\n\n*No common genes found across all assays.*\n"
                    header = "| Gene | " + " | ".join([f"Assay {i+1}" for i in range(len(assay_ids))]) + " |\n"
                    separator = "|" + "|".join(["---"] * (len(assay_ids) + 1)) + "|\n"
                    body_lines = [f"### {title}\n", header.rstrip("\n"), separator.rstrip("\n")]
                    for gene in sorted(common_genes):
                        values = [f"{per_assay_dict[aid][gene]:.3f}" for aid in assay_ids]
                        body_lines.append("| " + gene + " | " + " | ".join(values) + " |")
                    body_lines.append("")
                    body_lines.append(f"**{total_label}:** {len(common_genes)}")
                    return "\n".join(body_lines)

                # Upregulated genes
                up_title = f"Upregulated Genes (log2fc > {log2fc_threshold}, common across all assays)"
                up_table_md = _build_common_table_md(
                    up_title, common_up_genes, upregulated_genes,
                    "Total common upregulated genes"
                )
                up_csv_rows = _build_common_rows(common_up_genes, upregulated_genes)
                up_csv_path, up_csv_link = _write_results_csv(
                    up_csv_rows,
                    f"common_de_up_{'_'.join(a[:25] for a in assay_ids)}_l2fc{log2fc_threshold}_p{adj_p_threshold}"
                )
                up_csv_text = _render_csv_text(up_csv_rows)
                markdown_output += _canonical_block(
                    up_table_md, up_csv_path, up_csv_link,
                    context_label="common upregulated genes table",
                    csv_text=up_csv_text,
                ) + "\n\n"

                # Downregulated genes
                down_title = f"Downregulated Genes (log2fc < -{log2fc_threshold}, common across all assays)"
                down_table_md = _build_common_table_md(
                    down_title, common_down_genes, downregulated_genes,
                    "Total common downregulated genes"
                )
                down_csv_rows = _build_common_rows(common_down_genes, downregulated_genes)
                down_csv_path, down_csv_link = _write_results_csv(
                    down_csv_rows,
                    f"common_de_down_{'_'.join(a[:25] for a in assay_ids)}_l2fc{log2fc_threshold}_p{adj_p_threshold}"
                )
                down_csv_text = _render_csv_text(down_csv_rows)
                markdown_output += _canonical_block(
                    down_table_md, down_csv_path, down_csv_link,
                    context_label="common downregulated genes table",
                    csv_text=down_csv_text,
                ) + "\n\n"

                # Add assay ID reference
                markdown_output += "### Assay Reference\n\n"
                for i, assay_id in enumerate(assay_ids):
                    markdown_output += f"- **Assay {i+1}:** {assay_id}\n"

                return [types.TextContent(type="text", text=markdown_output, mimeType="text/markdown")]

            except Exception as e:
                logger.error(f"Error finding correlated differentially expressed genes: {e}")
                return [types.TextContent(
                    type="text", 
                    text=f"Error finding correlated differentially expressed genes: {e}"
                )]

    @mcp.tool(
        annotations={
            "title": "Find Common Differentially Methylated Regions (across assays)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_common_differentially_methylated_regions(
            assay_ids: list[str] = Field(..., description="List of assay identifiers for methylation comparisons"),
            methylation_diff_threshold: float = Field(0.0, description="Methylation difference threshold (default: 0.0 = any change)"),
            q_value_threshold: float = Field(0.05, description="q-value threshold for significance (default: 0.05)"),
            in_promoter: Optional[bool] = Field(
                None,
                description=(
                    "MethylationRegion location filter. When True, restrict to "
                    "regions overlapping a gene promoter. When False, exclude "
                    "promoter-overlapping regions. None (default) imposes no "
                    "promoter filter. Applied during the Cypher query — passing "
                    "in_promoter=True is the correct way to ask 'which genes are "
                    "commonly hypermethylated IN THE PROMOTER across these assays'."
                ),
            ),
            in_exon: Optional[bool] = Field(
                None,
                description="MethylationRegion location filter (True / False / None). See in_promoter.",
            ),
            in_intron: Optional[bool] = Field(
                None,
                description="MethylationRegion location filter (True / False / None). See in_promoter.",
            ),
            dist_to_feature_max: Optional[int] = Field(
                None,
                description=(
                    "MethylationRegion distance filter. When set, restrict to "
                    "regions with dist_to_feature <= this value (bp). None "
                    "(default) imposes no distance filter."
                ),
            ),
        ) -> list[types.TextContent]:
            """USE THIS TOOL (not the `query` tool) for any "common DMRs
            across assays", "genes hypermethylated in BOTH assays", "shared
            hyper/hypomethylated regions", or any cross-assay methylation
            intersection question — including promoter/exon/intron-filtered
            and distance-bounded variants. Falling back to `query` for this
            category reproduces the per-assay fetch + inner-join logic this
            tool already does correctly (parallel fetches, MethylationRegion
            location filtering applied during the Cypher so the common set
            reflects the filtered universe, methylation_diff/q_value
            thresholds, separate hyper vs. hypo intersections) and loses
            the formatted markdown + inline CSV side-channel and the
            assay-reference legend.

            Find common differentially methylated genes across multiple assays.

            This function:
            1. Takes a list of assay IDs as input (2 or more)
            2. Gets ALL genes associated with differentially methylated regions
               for each assay, applying the methylation_diff threshold,
               q_value threshold, and any MethylationRegion location filters
               (in_promoter, in_exon, in_intron, dist_to_feature_max)
            3. Inner joins among hypermethylated genes and among hypomethylated genes
            4. Returns markdown tables showing common genes and their methylation_diff values

            Use the location filter parameters rather than post-filtering — they
            are applied during the Cypher query so the common gene set reflects
            the filtered universe correctly.
            """

            if len(assay_ids) < 2:
                return [types.TextContent(type="text", text="Error: Please provide at least 2 assay IDs.")]

            try:
                hyper_genes = {}
                hypo_genes = {}

                # Build the MR filter block once and reuse across both queries.
                mr_filter_parts: list[str] = []
                mr_filter_params: dict[str, Any] = {}
                if in_promoter is not None:
                    mr_filter_parts.append("AND mr.in_promoter = $mr_in_promoter")
                    mr_filter_params["mr_in_promoter"] = in_promoter
                if in_exon is not None:
                    mr_filter_parts.append("AND mr.in_exon = $mr_in_exon")
                    mr_filter_params["mr_in_exon"] = in_exon
                if in_intron is not None:
                    mr_filter_parts.append("AND mr.in_intron = $mr_in_intron")
                    mr_filter_params["mr_in_intron"] = in_intron
                if dist_to_feature_max is not None:
                    mr_filter_parts.append(
                        "AND mr.dist_to_feature IS NOT NULL AND mr.dist_to_feature <= $mr_dist_max"
                    )
                    mr_filter_params["mr_dist_max"] = dist_to_feature_max
                mr_filter_block = (
                    "\n                  " + "\n                  ".join(mr_filter_parts)
                ) if mr_filter_parts else ""

                hyper_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE r.methylation_diff > $meth_threshold
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_threshold{mr_filter_block}
                RETURN DISTINCT mg.symbol AS gene_symbol, r.methylation_diff AS methylation_diff
                ORDER BY r.methylation_diff DESC
                """

                hypo_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE r.methylation_diff < -$meth_threshold
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_threshold{mr_filter_block}
                RETURN DISTINCT mg.symbol AS gene_symbol, r.methylation_diff AS methylation_diff
                ORDER BY r.methylation_diff ASC
                """

                # Parallel fetch across assays for the same reason as the DE genes tool.
                async def _fetch(aid, cypher):
                    params = {
                        "assay_id": aid,
                        "meth_threshold": methylation_diff_threshold,
                        "q_threshold": q_value_threshold,
                        **mr_filter_params,
                    }
                    async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                        result_json = await s.execute_read(_read, cypher, params)
                    return json.loads(result_json)

                hyper_tasks = [_fetch(aid, hyper_query) for aid in assay_ids]
                hypo_tasks = [_fetch(aid, hypo_query) for aid in assay_ids]
                hyper_results, hypo_results = await asyncio.gather(
                    asyncio.gather(*hyper_tasks),
                    asyncio.gather(*hypo_tasks),
                )
                for aid, hyper_data in zip(assay_ids, hyper_results):
                    hyper_genes[aid] = {row['gene_symbol']: row['methylation_diff'] for row in hyper_data}
                for aid, hypo_data in zip(assay_ids, hypo_results):
                    hypo_genes[aid] = {row['gene_symbol']: row['methylation_diff'] for row in hypo_data}
                
                common_hyper = set(hyper_genes[assay_ids[0]].keys())
                for aid in assay_ids[1:]:
                    common_hyper &= set(hyper_genes[aid].keys())
                
                common_hypo = set(hypo_genes[assay_ids[0]].keys())
                for aid in assay_ids[1:]:
                    common_hypo &= set(hypo_genes[aid].keys())
                
                md = f"## Common Differentially Methylated Genes\n\n"
                md += f"**Methylation Diff Threshold:** \u00b1{methylation_diff_threshold}\n"
                md += f"**q-value Threshold:** {q_value_threshold}\n\n"

                def _build_meth_rows(common_set, per_assay_dict):
                    rows = []
                    for gene in sorted(common_set):
                        row = {"gene_symbol": gene}
                        for i, aid in enumerate(assay_ids):
                            row[f"assay_{i+1}_methylation_diff"] = per_assay_dict[aid][gene]
                        rows.append(row)
                    return rows

                def _build_meth_table_md(title, common_set, per_assay_dict, total_label):
                    if not common_set:
                        return f"### {title}\n\n*No common genes found across all assays.*\n"
                    header = "| Gene | " + " | ".join([f"Assay {i+1}" for i in range(len(assay_ids))]) + " |"
                    sep = "|" + "|".join(["---"] * (len(assay_ids) + 1)) + "|"
                    body_lines = [f"### {title}\n", header, sep]
                    for gene in sorted(common_set):
                        vals = [f"{per_assay_dict[aid][gene]:.3f}" for aid in assay_ids]
                        body_lines.append("| " + gene + " | " + " | ".join(vals) + " |")
                    body_lines.append("")
                    body_lines.append(f"**{total_label}:** {len(common_set)}")
                    return "\n".join(body_lines)

                hyper_table_md = _build_meth_table_md(
                    "Hypermethylated Genes (common across all assays)",
                    common_hyper, hyper_genes, "Total common hypermethylated genes"
                )
                hyper_csv_rows = _build_meth_rows(common_hyper, hyper_genes)
                hyper_csv_path, hyper_csv_link = _write_results_csv(
                    hyper_csv_rows,
                    f"common_dm_hyper_{'_'.join(a[:25] for a in assay_ids)}_md{methylation_diff_threshold}_q{q_value_threshold}"
                )
                hyper_csv_text = _render_csv_text(hyper_csv_rows)
                md += _canonical_block(
                    hyper_table_md, hyper_csv_path, hyper_csv_link,
                    context_label="common hypermethylated genes table",
                    csv_text=hyper_csv_text,
                ) + "\n\n"

                hypo_table_md = _build_meth_table_md(
                    "Hypomethylated Genes (common across all assays)",
                    common_hypo, hypo_genes, "Total common hypomethylated genes"
                )
                hypo_csv_rows = _build_meth_rows(common_hypo, hypo_genes)
                hypo_csv_path, hypo_csv_link = _write_results_csv(
                    hypo_csv_rows,
                    f"common_dm_hypo_{'_'.join(a[:25] for a in assay_ids)}_md{methylation_diff_threshold}_q{q_value_threshold}"
                )
                hypo_csv_text = _render_csv_text(hypo_csv_rows)
                md += _canonical_block(
                    hypo_table_md, hypo_csv_path, hypo_csv_link,
                    context_label="common hypomethylated genes table",
                    csv_text=hypo_csv_text,
                ) + "\n\n"

                md += "### Assay Reference\n\n"
                for i, aid in enumerate(assay_ids):
                    md += f"- **Assay {i+1}:** {aid}\n"

                return [types.TextContent(type="text", text=md, mimeType="text/markdown")]

            except Exception as e:
                logger.error(f"Error finding common differentially methylated regions: {e}")
                return [types.TextContent(type="text", text=f"Error: {e}")]

    @mcp.tool(
        annotations={
            "title": "Find Common Differentially Abundant Organisms (across assays)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_common_differentially_abundant_organisms(
            assay_ids: list[str] = Field(..., description="List of assay identifiers for abundance comparisons (e.g., different methods like DESeq2, ANCOM-BC)"),
            log2fc_threshold: float = Field(0.0, description="Minimum |log2fc| magnitude for filtering organisms (default: 0.0 = any change). Applied to BOTH DESeq2 and ANCOM-BC rows since log2fc is populated for both methods."),
            q_value_threshold: float = Field(0.05, description="q-value threshold for ANCOM-BC abundance assays (default: 0.05). Applied to rows with q_value populated."),
            adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold for DESeq2 abundance assays (default: 0.05). Applied to rows with adj_p_value populated."),
            lnfc_threshold: Optional[float] = Field(None, description="Optional minimum |lnfc| magnitude. Only applied to rows with lnfc populated (ANCOM-BC). DESeq2 rows are not filtered by this parameter. Leave unset (None) to skip lnfc filtering entirely.")
        ) -> list[types.TextContent]:
            """USE THIS TOOL (not the `query` tool) for any "common
            differentially abundant organisms across assays", "organisms
            increased in BOTH assays", "shared abundance signals across
            methods" (e.g. DESeq2 vs ANCOM-BC concordance), or any cross-assay
            abundance intersection question. Falling back to `query` for this
            category reproduces the per-assay fetch + inner-join logic this
            tool already does correctly (parallel fetches, method-aware
            significance filtering, lnfc handling that doesn't drop DESeq2
            rows, separate increased vs. decreased intersections) and loses
            the formatted markdown + inline CSV side-channel and the
            assay-reference legend.

            Find common differentially abundant organisms across multiple assays.

            Useful for comparing results from different analysis methods (e.g., DESeq2 vs ANCOM-BC 1 vs ANCOM-BC 2)
            to identify organisms consistently detected as differentially abundant.

            Direction (increase vs. decrease) and ranking use log2fc, which is populated for
            BOTH DESeq2 and ANCOM-BC edges in the GeneLab KG. log2fc is therefore the
            universal magnitude filter (log2fc_threshold); lnfc is ANCOM-BC-only and
            would null-filter every DESeq2 row if used for direction.

            Significance filtering is method-aware:
              - DESeq2 edges populate adj_p_value (not q_value); the adj_p_threshold applies.
              - ANCOM-BC edges populate q_value (not adj_p_value); the q_value_threshold applies.
            Each row is kept if its populated significance field meets the matching threshold.

            Magnitude filtering:
              - log2fc_threshold (default 0.0) applies to every row, since log2fc is populated for both methods.
              - lnfc_threshold (default None = no filter) is method-aware: when set, it requires
                |lnfc| >= lnfc_threshold on rows where lnfc is populated. Rows where lnfc is null
                (i.e., DESeq2 rows) are not affected by this filter.

            This function:
            1. Takes a list of assay IDs as input (2 or more)
            2. Gets ALL organisms passing the magnitude and method-aware significance filters for each assay
            3. Inner joins among increased and among decreased abundance organisms
            4. Returns markdown tables showing common organisms and their log2fc values
            """

            if len(assay_ids) < 2:
                return [types.TextContent(type="text", text="Error: Please provide at least 2 assay IDs.")]

            try:
                increased_organisms = {}
                decreased_organisms = {}

                # Conditionally include the lnfc magnitude clause. Built once before the
                # query strings so it's identical across both queries and both directions.
                if lnfc_threshold is None:
                    lnfc_clause = ""
                else:
                    lnfc_clause = (
                        "                  AND (r.lnfc IS NULL OR abs(r.lnfc) >= $lnfc_threshold)\n"
                    )

                up_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
                      (o:Organism)
                WHERE r.log2fc IS NOT NULL AND r.log2fc > $log2fc_threshold
                  AND (
                    (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
                    OR (r.q_value IS NOT NULL AND r.q_value <= $q_threshold)
                  )
{lnfc_clause}                RETURN o.name AS organism_name, r.log2fc AS log2fc
                ORDER BY r.log2fc DESC
                """

                down_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
                      (o:Organism)
                WHERE r.log2fc IS NOT NULL AND r.log2fc < -$log2fc_threshold
                  AND (
                    (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
                    OR (r.q_value IS NOT NULL AND r.q_value <= $q_threshold)
                  )
{lnfc_clause}                RETURN o.name AS organism_name, r.log2fc AS log2fc
                ORDER BY r.log2fc ASC
                """

                # Parallel fetch across assays for the same reason as the DE tool.
                def _params_for(aid):
                    p = {
                        "assay_id": aid,
                        "log2fc_threshold": log2fc_threshold,
                        "q_threshold": q_value_threshold,
                        "adj_p_threshold": adj_p_threshold,
                    }
                    if lnfc_threshold is not None:
                        p["lnfc_threshold"] = lnfc_threshold
                    return p

                async def _fetch(aid, cypher):
                    async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                        result_json = await s.execute_read(_read, cypher, _params_for(aid))
                    return json.loads(result_json)

                up_tasks = [_fetch(aid, up_query) for aid in assay_ids]
                down_tasks = [_fetch(aid, down_query) for aid in assay_ids]
                up_results, down_results = await asyncio.gather(
                    asyncio.gather(*up_tasks),
                    asyncio.gather(*down_tasks),
                )
                for aid, up_data in zip(assay_ids, up_results):
                    increased_organisms[aid] = {row['organism_name']: row['log2fc'] for row in up_data}
                for aid, down_data in zip(assay_ids, down_results):
                    decreased_organisms[aid] = {row['organism_name']: row['log2fc'] for row in down_data}

                common_up = set(increased_organisms[assay_ids[0]].keys())
                for aid in assay_ids[1:]:
                    common_up &= set(increased_organisms[aid].keys())

                common_down = set(decreased_organisms[assay_ids[0]].keys())
                for aid in assay_ids[1:]:
                    common_down &= set(decreased_organisms[aid].keys())

                md = f"## Common Differentially Abundant Organisms\n\n"
                md += f"**Log2FC Threshold:** \u00b1{log2fc_threshold}\n"
                md += f"**q-value Threshold (ANCOM-BC rows):** {q_value_threshold}\n"
                md += f"**Adj. p-value Threshold (DESeq2 rows):** {adj_p_threshold}\n"
                if lnfc_threshold is not None:
                    md += f"**|LnFC| Threshold (ANCOM-BC rows only):** {lnfc_threshold}\n"
                md += "\n"

                def _build_abund_rows(common_set, per_assay_dict):
                    rows = []
                    for org in sorted(common_set):
                        row = {"organism_name": org}
                        for i, aid in enumerate(assay_ids):
                            row[f"assay_{i+1}_log2fc"] = per_assay_dict[aid][org]
                        rows.append(row)
                    return rows

                def _build_abund_table_md(title, common_set, per_assay_dict, total_label):
                    if not common_set:
                        return f"### {title}\n\n*No common organisms found across all assays.*\n"
                    header = "| Organism | " + " | ".join([f"Assay {i+1} (log2fc)" for i in range(len(assay_ids))]) + " |"
                    sep = "|" + "|".join(["---"] * (len(assay_ids) + 1)) + "|"
                    body_lines = [f"### {title}\n", header, sep]
                    for org in sorted(common_set):
                        vals = [f"{per_assay_dict[aid][org]:.4f}" for aid in assay_ids]
                        body_lines.append("| " + org + " | " + " | ".join(vals) + " |")
                    body_lines.append("")
                    body_lines.append(f"**{total_label}:** {len(common_set)}")
                    return "\n".join(body_lines)

                up_table_md = _build_abund_table_md(
                    "Increased Abundance (common across all assays)",
                    common_up, increased_organisms, "Total common increased"
                )
                up_csv_rows = _build_abund_rows(common_up, increased_organisms)
                up_csv_path, up_csv_link = _write_results_csv(
                    up_csv_rows,
                    f"common_da_up_{'_'.join(a[:25] for a in assay_ids)}_l2fc{log2fc_threshold}"
                )
                up_csv_text = _render_csv_text(up_csv_rows)
                md += up_table_md + "\n"
                if up_csv_path:
                    md += f"\n_CSV: `{up_csv_path}`_\n\n"
                else:
                    md += "\n"

                down_table_md = _build_abund_table_md(
                    "Decreased Abundance (common across all assays)",
                    common_down, decreased_organisms, "Total common decreased"
                )
                down_csv_rows = _build_abund_rows(common_down, decreased_organisms)
                down_csv_path, down_csv_link = _write_results_csv(
                    down_csv_rows,
                    f"common_da_down_{'_'.join(a[:25] for a in assay_ids)}_l2fc{log2fc_threshold}"
                )
                down_csv_text = _render_csv_text(down_csv_rows)
                md += down_table_md + "\n"
                if down_csv_path:
                    md += f"\n_CSV: `{down_csv_path}`_\n\n"
                else:
                    md += "\n"

                md += "### Assay Reference\n\n"
                for i, aid in enumerate(assay_ids):
                    md += f"- **Assay {i+1}:** {aid}\n"

                return [types.TextContent(type="text", text=md, mimeType="text/markdown")]

            except Exception as e:
                logger.error(f"Error finding common differentially abundant organisms: {e}")
                return [types.TextContent(type="text", text=f"Error: {e}")]

    @mcp.tool(
        annotations={
            "title": "Find DE Genes Overlapping DM Regions (expression-methylation coupling)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def find_common_de_genes_overlapping_dm_regions(
            expression_assay_id: str = Field(..., description="Assay identifier for differential expression data"),
            methylation_assay_id: "str | list[str]" = Field(
                ...,
                description=(
                    "Assay identifier for differential methylation data, or a "
                    "list of identifiers to pool. Pass a single string to use "
                    "one methylation assay (most common). Pass a list (e.g. "
                    "['OSD-48-968c...', 'OSD-48-4dc1...']) to pool methylation "
                    "evidence across replicate methylation assays of the same "
                    "comparison — a gene is treated as 'hypermethylated' if "
                    "ANY of the pooled assays passes the threshold for it. "
                    "This matches the natural reading of 'find DE genes that "
                    "have hypermethylated promoter evidence in this study's "
                    "methylation assays', where the methylation side is a "
                    "union across assays before being intersected with the DE "
                    "side."
                ),
            ),
            log2fc_threshold: float = Field(1.0, description="Log2 fold change threshold for DE genes (default: 1.0)"),
            adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold for DE genes (default: 0.05)"),
            methylation_diff_threshold: float = Field(0.0, description="Methylation diff threshold for DM regions (default: 0.0)"),
            q_value_threshold: float = Field(0.05, description="q-value threshold for DM regions (default: 0.05)"),
            in_promoter: Optional[bool] = Field(
                None,
                description=(
                    "MethylationRegion location filter applied to the DM side of "
                    "the overlap. When True, only DM regions overlapping a "
                    "promoter contribute to the methylated-gene set. Use this "
                    "for classical epigenetic-silencing questions: 'which "
                    "downregulated genes have hypermethylation in their "
                    "promoter?' → pass in_promoter=True."
                ),
            ),
            in_exon: Optional[bool] = Field(
                None,
                description="MethylationRegion location filter (True / False / None). See in_promoter.",
            ),
            in_intron: Optional[bool] = Field(
                None,
                description="MethylationRegion location filter (True / False / None). See in_promoter.",
            ),
            dist_to_feature_max: Optional[int] = Field(
                None,
                description=(
                    "MethylationRegion distance filter. When set, only DM regions "
                    "with dist_to_feature <= this value (bp) contribute to the "
                    "methylated-gene set. None (default) imposes no distance filter."
                ),
            ),
        ) -> list[types.TextContent]:
            """USE THIS TOOL (not the `query` tool) for any
            "expression-methylation coupling", "classical epigenetic
            silencing", "DE genes that are also DM", "downregulated genes
            with promoter hypermethylation", or any cross-type
            intersection between differential expression and differential
            methylation. Falling back to `query` for this category reproduces
            the four-quadrant intersection logic this tool already does
            correctly (parallel DE+DM fetches across DIFFERENT assays,
            pooled-methylation aggregation when multiple methylation assays
            are passed as a list, MethylationRegion location filtering on the
            DM side, up_hyper/up_hypo/down_hyper/down_hypo direction
            categorization) and loses the four-quadrant markdown summary and
            the inline combined CSV.

            Find differentially expressed genes that overlap with differentially methylated regions.

            This cross-analysis tool:
            1. Gets DE genes from the expression assay (filtered by log2fc and adj_p thresholds)
            2. Gets genes associated with DM regions from the methylation assay(s)
               (filtered by methylation_diff, q_value, and any MethylationRegion
               location filters: in_promoter, in_exon, in_intron, dist_to_feature_max).
               If `methylation_assay_id` is a list, the methylation evidence is
               POOLED across the listed assays before the intersection — a gene
               is treated as hypermethylated if it passes the threshold in ANY
               of the pooled assays.
            3. Finds the intersection (genes that are BOTH differentially expressed AND differentially methylated)
            4. Reports the overlap categorized by direction (up+hyper, up+hypo, down+hyper, down+hypo)

            For the classical epigenetic-silencing question — promoter
            hypermethylation reducing expression — pass in_promoter=True.
            When a study has replicate methylation assays (e.g. OSD-48 has
            two methylKit assays for the same Space Flight vs. Ground Control
            comparison), pass them as a list to capture the combined methylation
            evidence on the methylation side of the intersection.
            """

            # Normalize methylation_assay_id to a list so pooled and single
            # paths share one implementation.
            if isinstance(methylation_assay_id, str):
                methylation_assay_ids = [methylation_assay_id]
            else:
                methylation_assay_ids = list(methylation_assay_id)
            if not methylation_assay_ids:
                return [types.TextContent(
                    type="text",
                    text="Error: at least one methylation_assay_id must be provided.",
                )]

            try:
                # Build the MR filter block once and reuse across both DM queries.
                mr_filter_parts: list[str] = []
                mr_filter_params: dict[str, Any] = {}
                if in_promoter is not None:
                    mr_filter_parts.append("AND mr.in_promoter = $mr_in_promoter")
                    mr_filter_params["mr_in_promoter"] = in_promoter
                if in_exon is not None:
                    mr_filter_parts.append("AND mr.in_exon = $mr_in_exon")
                    mr_filter_params["mr_in_exon"] = in_exon
                if in_intron is not None:
                    mr_filter_parts.append("AND mr.in_intron = $mr_in_intron")
                    mr_filter_params["mr_in_intron"] = in_intron
                if dist_to_feature_max is not None:
                    mr_filter_parts.append(
                        "AND mr.dist_to_feature IS NOT NULL AND mr.dist_to_feature <= $mr_dist_max"
                    )
                    mr_filter_params["mr_dist_max"] = dist_to_feature_max
                mr_filter_block = (
                    "\n                  " + "\n                  ".join(mr_filter_parts)
                ) if mr_filter_parts else ""

                # Get DE genes
                up_de_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (mg:MGene)
                WHERE r.log2fc > $log2fc_threshold
                  AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
                RETURN mg.symbol AS gene_symbol, r.log2fc AS log2fc
                """

                down_de_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (mg:MGene)
                WHERE r.log2fc < -$log2fc_threshold
                  AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
                RETURN mg.symbol AS gene_symbol, r.log2fc AS log2fc
                """

                # Get DM genes. Both queries use `a.identifier IN $assay_ids`
                # so pooled and single-assay calls share the same Cypher path —
                # the gene set returned is the UNION across assays (a gene
                # appears once with the most-extreme methylation_diff seen).
                # The aggregation (max for hyper, min for hypo) captures the
                # strongest evidence across pooled assays per gene.
                hyper_dm_query = f"""
                MATCH (a:Assay)
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE a.identifier IN $assay_ids
                  AND r.methylation_diff > $meth_threshold
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_threshold{mr_filter_block}
                RETURN mg.symbol AS gene_symbol, max(r.methylation_diff) AS methylation_diff
                """

                hypo_dm_query = f"""
                MATCH (a:Assay)
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE a.identifier IN $assay_ids
                  AND r.methylation_diff < -$meth_threshold
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_threshold{mr_filter_block}
                RETURN mg.symbol AS gene_symbol, min(r.methylation_diff) AS methylation_diff
                """

                de_params = {"assay_id": expression_assay_id, "log2fc_threshold": log2fc_threshold, "adj_p_threshold": adj_p_threshold}
                dm_params = {"assay_ids": methylation_assay_ids, "meth_threshold": methylation_diff_threshold, "q_threshold": q_value_threshold, **mr_filter_params}
                
                # Run all 4 reads concurrently — DE and DM queries are independent
                # and use different assays.
                async def _run(cypher, params):
                    async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                        return await s.execute_read(_read, cypher, params)

                up_de_result, down_de_result, hyper_dm_result, hypo_dm_result = await asyncio.gather(
                    _run(up_de_query, de_params),
                    _run(down_de_query, de_params),
                    _run(hyper_dm_query, dm_params),
                    _run(hypo_dm_query, dm_params),
                )

                up_de = {r['gene_symbol']: r['log2fc'] for r in json.loads(up_de_result)}
                down_de = {r['gene_symbol']: r['log2fc'] for r in json.loads(down_de_result)}
                hyper_dm = {r['gene_symbol']: r['methylation_diff'] for r in json.loads(hyper_dm_result)}
                hypo_dm = {r['gene_symbol']: r['methylation_diff'] for r in json.loads(hypo_dm_result)}
                
                # Find overlaps in all 4 combinations
                up_hyper = set(up_de.keys()) & set(hyper_dm.keys())
                up_hypo = set(up_de.keys()) & set(hypo_dm.keys())
                down_hyper = set(down_de.keys()) & set(hyper_dm.keys())
                down_hypo = set(down_de.keys()) & set(hypo_dm.keys())
                
                all_de = set(up_de.keys()) | set(down_de.keys())
                all_dm = set(hyper_dm.keys()) | set(hypo_dm.keys())
                total_overlap = all_de & all_dm
                
                md = f"## DE Genes Overlapping DM Regions\n\n"
                md += f"**Expression Assay:** `{expression_assay_id}`\n"
                if len(methylation_assay_ids) == 1:
                    md += f"**Methylation Assay:** `{methylation_assay_ids[0]}`\n\n"
                else:
                    assay_list_md = ", ".join(f"`{a}`" for a in methylation_assay_ids)
                    md += f"**Methylation Assays (pooled across {len(methylation_assay_ids)}):** {assay_list_md}\n\n"
                md += f"**DE Thresholds:** |log2fc| > {log2fc_threshold}, adj_p < {adj_p_threshold}\n"
                md += f"**DM Thresholds:** |methylation_diff| > {methylation_diff_threshold}, q_value < {q_value_threshold}\n\n"
                md += f"**Summary:** {len(all_de)} DE genes, {len(all_dm)} DM genes, **{len(total_overlap)} overlapping**\n\n"

                def _overlap_table(genes, title, de_dict, dm_dict, de_col, dm_col):
                    if not genes:
                        return f"### {title}\n*No overlapping genes found.*"
                    lines = [f"### {title} ({len(genes)} genes)", "",
                             f"| Gene | {de_col} | {dm_col} |",
                             "|------|---------|---------|"]
                    for g in sorted(genes):
                        de_val = de_dict.get(g, 'N/A')
                        dm_val = dm_dict.get(g, 'N/A')
                        de_str = f"{de_val:.3f}" if isinstance(de_val, (int, float)) else de_val
                        dm_str = f"{dm_val:.3f}" if isinstance(dm_val, (int, float)) else dm_val
                        lines.append(f"| {g} | {de_str} | {dm_str} |")
                    return "\n".join(lines)

                def _overlap_rows(genes, de_dict, dm_dict, category):
                    rows = []
                    for g in sorted(genes):
                        rows.append({
                            "gene_symbol": g,
                            "category": category,
                            "log2fc": de_dict.get(g),
                            "methylation_diff": dm_dict.get(g),
                        })
                    return rows

                # Build one combined CSV across all four categories so the user has a
                # single canonical artifact for the whole overlap analysis.
                all_overlap_rows = (
                    _overlap_rows(up_hyper, up_de, hyper_dm, "up_hyper") +
                    _overlap_rows(up_hypo, up_de, hypo_dm, "up_hypo") +
                    _overlap_rows(down_hyper, down_de, hyper_dm, "down_hyper") +
                    _overlap_rows(down_hypo, down_de, hypo_dm, "down_hypo")
                )
                # CSV filename stem: include the methylation pool size when
                # pooled so the saved file reflects the actual analysis.
                if len(methylation_assay_ids) == 1:
                    meth_stem = methylation_assay_ids[0][:30]
                else:
                    meth_stem = f"pooled{len(methylation_assay_ids)}_{methylation_assay_ids[0][:24]}"
                combined_csv_path, combined_csv_link = _write_results_csv(
                    all_overlap_rows,
                    f"de_dm_overlap_{expression_assay_id[:30]}_vs_{meth_stem}"
                )
                combined_csv_text = _render_csv_text(all_overlap_rows)

                quadrants = [
                    (up_hyper, "Upregulated & Hypermethylated", up_de, hyper_dm, "up_hyper"),
                    (up_hypo, "Upregulated & Hypomethylated", up_de, hypo_dm, "up_hypo"),
                    (down_hyper, "Downregulated & Hypermethylated", down_de, hyper_dm, "down_hyper"),
                    (down_hypo, "Downregulated & Hypomethylated", down_de, hypo_dm, "down_hypo"),
                ]
                for idx, (genes, title, de_dict, dm_dict, label) in enumerate(quadrants):
                    table_md = _overlap_table(genes, title, de_dict, dm_dict, "Log2FC", "Meth Diff (%)")
                    # The combined CSV covers all 4 quadrants (it has a `category`
                    # column to filter on). Emit it ONLY with the first table to
                    # avoid 4× duplicating the same CSV bytes in the response.
                    # Subsequent tables show just the markdown table and refer
                    # back to the same CSV.
                    if idx == 0:
                        md += _canonical_block(
                            table_md, combined_csv_path, combined_csv_link,
                            context_label=f"{label} overlap table",
                            csv_text=combined_csv_text,
                        ) + "\n\n"
                    else:
                        md += table_md + "\n\n"

                return [types.TextContent(type="text", text=md, mimeType="text/markdown")]

            except Exception as e:
                logger.error(f"Error finding DE/DM overlap: {e}")
                return [types.TextContent(type="text", text=f"Error: {e}")]

    
    @mcp.tool(
        annotations={
            "title": "Create Volcano Plot (expression / methylation / abundance)",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def create_volcano_plot(
        assay_id: str = Field(..., description="Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')"),
        data_type: str = Field("expression", description="Which kind of differential data to plot. Options: 'expression' (differentially expressed genes), 'methylation' (differentially methylated regions), 'abundance' (differentially abundant organisms; works for both DESeq2 and ANCOM-BC). Default: 'expression'."),
        log2fc_threshold: float = Field(1.0, description="Log2 fold change threshold (default 1.0 = 2-fold). Applies to data_type='expression' and data_type='abundance'. Ignored for data_type='methylation' (use methylation_diff_threshold instead)."),
        methylation_diff_threshold: float = Field(10.0, description="Methylation difference threshold in percentage points (default 10.0 = |diff| > 10%). Applies only to data_type='methylation'. Ignored for other data types."),
        adj_p_threshold: float = Field(0.05, description="Adjusted p-value (DESeq2) / q-value (ANCOM-BC, methylation) threshold for significance. For abundance, both fields are checked in a method-aware way."),
        top_n: int = Field(20, description="How many significant points to label in the plot (selected by smallest p/q-value)."),
        figsize_width: int = Field(8, description="Figure width in inches"),
        figsize_height: int = Field(5, description="Figure height in inches"),
        label_avoid_overlap: bool = Field(True, description="If True, use adjustText to reposition labels to avoid overlap. Disable on very large assays for faster rendering."),
    ) -> list[types.Content]:
        """Create a volcano plot for differential data from the given assay.

        Works for three differential measurement types:
          - 'expression':  -[MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]-> MGene
                            x = log2fc, y = -log10(adj_p_value), labels = gene symbol
                            Magnitude filter: log2fc_threshold (default 1.0 = 2-fold)
          - 'methylation': -[MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]-> MethylationRegion
                            x = methylation_diff, y = -log10(q_value), labels = gene symbol
                            (joined via METHYLATED_IN_MGmMR back to MGene for labeling)
                            Magnitude filter: methylation_diff_threshold (default 10.0 = 10%)
          - 'abundance':   -[MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]-> Organism
                            x = log2fc, y = -log10(significance), labels = organism name
                            Magnitude filter: log2fc_threshold (default 1.0 = 2-fold)
                            Significance is method-aware: DESeq2 rows use adj_p_value,
                            ANCOM-BC rows use q_value.

        Points are colored by significance:
          - Red:  positive direction (upregulated / hypermethylated / increased)
          - Blue: negative direction (downregulated / hypomethylated / decreased)
          - Gray: not significant

        Returns a markdown summary, an inline PNG image (if small enough), and the
        saved file path. Designed to handle assays with thousands of features without
        timing out.
        """

        valid_types = ("expression", "methylation", "abundance")
        if data_type not in valid_types:
            return [types.TextContent(
                type="text",
                text=f"Error: data_type must be one of {valid_types}. Got: '{data_type}'"
            )]

        # ---- Pick the magnitude threshold that applies to this data type ------
        # Methylation is in percentage-point units (typically -100 to +100) and a
        # sensible default highlight cutoff is |diff| > 10%. Expression and
        # abundance share a log2 fold change axis where |log2fc| > 1.0 (2-fold)
        # is the conventional default. Using a single overloaded parameter would
        # silently misapply units when the user switches data_type, so we keep
        # them separate and pick the right one here.
        if data_type == "methylation":
            magnitude_threshold = methylation_diff_threshold
        else:
            magnitude_threshold = log2fc_threshold

        # ---- Cypher queries, keyed by data_type --------------------------------
        # In every case we ask the database to do the filtering (IS NOT NULL guards)
        # so we never hand NaN/None into numpy.
        if data_type == "expression":
            data_cypher = """
            MATCH (a:Assay {identifier: $assay_id})
                  -[m:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                  (g:MGene)
            WHERE m.log2fc IS NOT NULL AND m.adj_p_value IS NOT NULL
            RETURN
              g.symbol       AS label,
              m.log2fc       AS x,
              m.adj_p_value  AS p
            """
            x_axis_label = "Log₂ Fold Change"
            y_axis_label = "-Log₁₀ (Adjusted P-value)"
            positive_label = "Upregulated"
            negative_label = "Downregulated"
            plot_title_prefix = "Volcano Plot (Expression)"

        elif data_type == "methylation":
            # Join through the gene so we can label points with gene symbol, which is
            # more biologically useful than the region identifier. Regions without an
            # associated gene are still included (label may be null).
            data_cypher = """
            MATCH (a:Assay {identifier: $assay_id})
                  -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                  (mr:MethylationRegion)
            OPTIONAL MATCH (mr)<-[:METHYLATED_IN_MGmMR]-(g:MGene)
            WHERE r.methylation_diff IS NOT NULL AND r.q_value IS NOT NULL
            RETURN
              coalesce(g.symbol, mr.identifier) AS label,
              r.methylation_diff                AS x,
              r.q_value                         AS p
            """
            x_axis_label = "Methylation Difference (%)"
            y_axis_label = "-Log₁₀ (q-value)"
            positive_label = "Hypermethylated"
            negative_label = "Hypomethylated"
            plot_title_prefix = "Volcano Plot (Methylation)"

        else:  # abundance
            # Method-aware: DESeq2 rows have adj_p_value populated, ANCOM-BC rows have
            # q_value populated. Build a single significance column by coalescing.
            # This is consistent with how every other abundance tool in this file works.
            data_cypher = """
            MATCH (a:Assay {identifier: $assay_id})
                  -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
                  (o:Organism)
            WHERE r.log2fc IS NOT NULL
              AND (r.adj_p_value IS NOT NULL OR r.q_value IS NOT NULL)
            RETURN
              o.name                                   AS label,
              r.log2fc                                 AS x,
              coalesce(r.adj_p_value, r.q_value)       AS p
            """
            x_axis_label = "Log₂ Fold Change"
            y_axis_label = "-Log₁₀ (Significance)"
            positive_label = "Increased Abundance"
            negative_label = "Decreased Abundance"
            plot_title_prefix = "Volcano Plot (Abundance)"

        meta_cypher = """
        MATCH (a:Assay {identifier: $assay_id})
        RETURN a.factors_1 AS factors_1, a.factors_2 AS factors_2
        """

        try:
            # ---- Fetch data ---------------------------------------------------
            # Run the data query and the metadata query concurrently — they're
            # independent and the metadata fetch is small so it never delays
            # the larger data query.
            async def _run(cypher):
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, cypher, {"assay_id": assay_id})

            data_json_str, meta_json_str = await asyncio.gather(
                _run(data_cypher),
                _run(meta_cypher),
            )

            rows = json.loads(data_json_str)
            meta_data = json.loads(meta_json_str)

            if not rows:
                return [types.TextContent(
                    type="text",
                    text=f"No {data_type} data found for assay: {assay_id}",
                )]

            if not meta_data:
                # The data query returned rows but the metadata query found no assay —
                # would normally only happen if the KG has a dangling reference. Use
                # generic labels and continue rather than crashing.
                factors_1 = []
                factors_2 = []
            else:
                factors_1 = meta_data[0].get('factors_1') or []
                factors_2 = meta_data[0].get('factors_2') or []
            # Filter out any None elements before joining (defensive: the KG should
            # never store nulls inside the list, but if it does, ",".join fails).
            factors_1 = [str(x) for x in factors_1 if x is not None]
            factors_2 = [str(x) for x in factors_2 if x is not None]
            factors_1_str = ",".join(factors_1) if factors_1 else "Group 1"
            factors_2_str = ",".join(factors_2) if factors_2 else "Group 2"
            study = "-".join(assay_id.split("-")[:2])

            # ---- Vectorize into numpy arrays once -----------------------------
            labels = np.array([r.get('label') for r in rows], dtype=object)
            x_vals = np.array([r['x'] for r in rows], dtype=float)
            p_vals = np.array([r['p'] for r in rows], dtype=float)

            # -log10 of a tiny floor instead of 0 to avoid +inf without the
            # awkward replace-then-add-1 pattern. Floor at the smallest positive
            # p value in the set (or 1e-300 if all are zero) so the y-axis scaling
            # remains data-driven.
            pos_p = p_vals[p_vals > 0]
            p_floor = float(pos_p.min()) if pos_p.size else 1e-300
            p_safe = np.maximum(p_vals, p_floor)
            neg_log10_p = -np.log10(p_safe)

            # ---- Classify (vectorized) ---
            sig_p = p_vals <= adj_p_threshold
            sig_up = sig_p & (x_vals > magnitude_threshold)
            sig_down = sig_p & (x_vals < -magnitude_threshold)
            not_sig = ~(sig_up | sig_down)

            n_sig_up = int(sig_up.sum())
            n_sig_down = int(sig_down.sum())
            n_not_sig = int(not_sig.sum())

            # ---- Draw ---------------------------------------------------------
            # Wrap the entire figure lifecycle in try/finally so that the matplotlib
            # figure is ALWAYS closed, even if drawing, adjust_text, or savefig
            # raises. Leaked figures accumulate in matplotlib's global state and
            # slow down subsequent calls; this was a possible contributor to the
            # 4-minute timeout reported on the OSD-244 30-day assay where many
            # volcano plot calls happened in sequence.
            fig, ax = plt.subplots(figsize=(figsize_width, figsize_height))
            try:
                # Not-significant first (gray), then colored points on top.
                ax.scatter(x_vals[not_sig], neg_log10_p[not_sig],
                           c='lightgray', alpha=0.5, s=10,
                           label=f'Not significant ({n_not_sig})')
                ax.scatter(x_vals[sig_down], neg_log10_p[sig_down],
                           c='#3498db', alpha=0.7, s=20,
                           label=f'{negative_label} ({n_sig_down})')
                ax.scatter(x_vals[sig_up], neg_log10_p[sig_up],
                           c='#e74c3c', alpha=0.7, s=20,
                           label=f'{positive_label} ({n_sig_up})')

                # ---- Pick top_n labels: smallest p among significant points ---
                # np.argsort on neg_log10_p (descending) and take top_n that are sig.
                # O(n log n) on numpy arrays.
                sig_mask = sig_up | sig_down
                sig_idx = np.where(sig_mask)[0]
                if sig_idx.size and top_n > 0:
                    order = sig_idx[np.argsort(-neg_log10_p[sig_idx])]
                    top_indices = order[:top_n]
                else:
                    top_indices = np.array([], dtype=int)

                texts = []
                for i in top_indices:
                    lbl = labels[i]
                    if lbl is None or (isinstance(lbl, float) and np.isnan(lbl)):
                        continue
                    t = ax.text(
                        float(x_vals[i]), float(neg_log10_p[i]), str(lbl),
                        fontsize=8, alpha=0.85,
                        bbox=dict(boxstyle='round,pad=0.3',
                                  facecolor='white', edgecolor='none', alpha=0.6),
                    )
                    texts.append(t)

                # ---- Avoid label overlap, but with a tight iteration cap ------
                # The old code used lim=500. With ~3000 significant scatter points
                # that meant ~30M geometry checks per label, which can run for many
                # minutes. Cap at 50 iterations and let label_avoid_overlap=False
                # be an escape hatch for users on very large assays.
                if label_avoid_overlap and texts:
                    try:
                        adjust_text(
                            texts,
                            arrowprops=dict(arrowstyle='->', color='gray', lw=0.5,
                                            alpha=0.7, shrinkA=0, shrinkB=2,
                                            relpos=(0.5, 0.5)),
                            expand_points=(1.5, 1.8),
                            expand_text=(1.3, 1.6),
                            force_text=(0.5, 0.8),
                            force_points=(0.2, 0.4),
                            lim=50,
                            ax=ax,
                        )
                    except Exception as adjust_err:
                        # adjustText sometimes raises on degenerate input — never
                        # let it tank the whole plot. Labels will overlap but the
                        # plot still renders.
                        logger.warning(f"adjust_text failed, falling back to raw labels: {adjust_err}")

                # ---- Threshold guide lines ------------------------------------
                ax.axvline(x=magnitude_threshold, color='gray', linestyle='--', linewidth=1, alpha=0.5)
                ax.axvline(x=-magnitude_threshold, color='gray', linestyle='--', linewidth=1, alpha=0.5)
                ax.axhline(y=-np.log10(adj_p_threshold), color='gray', linestyle='--', linewidth=1, alpha=0.5)

                ax.set_xlabel(x_axis_label, fontsize=12, fontweight='bold')
                ax.set_ylabel(y_axis_label, fontsize=12, fontweight='bold')
                ax.text(0.5, 1.08, f'{plot_title_prefix}: {study}',
                        transform=ax.transAxes, fontsize=12, fontweight='bold',
                        ha='center', va='bottom')
                ax.text(0.5, 1.03, f'({factors_1_str}) vs. ({factors_2_str})',
                        transform=ax.transAxes, fontsize=10, ha='center', va='bottom')

                ax.legend(loc='upper left', fontsize=8, bbox_to_anchor=(1, 1),
                          frameon=True, fancybox=True, shadow=True)
                ax.grid(True, alpha=0.3, linestyle=':', linewidth=0.5)

                plt.tight_layout()

                # ---- Save to disk --------------------------------------------
                # ---- Save plot to memory + (when reachable) to disk --------
                # Always render to an in-memory BytesIO so we have the PNG bytes
                # for both: (a) inline ImageContent in the response, and (b) the
                # embedded "Save to your local machine" command. Then write to
                # the resolved disk path so users on stdio/local deployments
                # also get a direct file at the user-facing location.
                import io as _io
                png_buf = _io.BytesIO()
                plt.savefig(png_buf, format='png', dpi=120, bbox_inches='tight')
                png_buf.seek(0)
                png_bytes = png_buf.getvalue()

                stem = f'volcano_plot_{study}_{data_type}_{factors_1_str}_vs_{factors_2_str}'
                write_path, user_facing_path, download_link = _resolve_output_paths(
                    stem, extension="png"
                )
                # Only write the PNG to the server's filesystem in stdio/local
                # mode where the user can actually reach it. In AgentCore /
                # remote deployments the inline ImageContent + on-demand
                # get_save_script is the user's reachable copy; writing to
                # /tmp inside the microVM is dead I/O that leaks across
                # stateful sessions.
                if not _is_remote_deployment():
                    try:
                        os.makedirs(os.path.dirname(write_path), exist_ok=True)
                        with open(write_path, "wb") as f:
                            f.write(png_bytes)
                        logger.info(f"Volcano plot saved to {write_path}")
                    except Exception as save_err:
                        logger.warning(f"Could not save volcano plot to {write_path}: {save_err}")
                        # Even if disk write failed, png_bytes is still available
                        # for inline + the embedded save command.
                    write_path = None
            finally:
                # Always close the figure to prevent matplotlib memory leaks.
                plt.close(fig)

            # ---- Build response ----------------------------------------------
            # Show the threshold with its actual unit so users immediately see the
            # difference between "log2fc > 1" for expression/abundance and
            # "diff > 10%" for methylation.
            if data_type == "methylation":
                threshold_label = f"|methylation_diff| > {magnitude_threshold}%"
            else:
                threshold_label = f"|log2fc| > {magnitude_threshold}"
            summary = (
                f"## Volcano Plot Generated\n\n"
                f"**Assay:** {assay_id}\n\n"
                f"**Data Type:** {data_type}\n\n"
                f"**Factors:** {factors_1_str} vs. {factors_2_str}\n\n"
                f"**Thresholds:**\n"
                f"- {threshold_label}\n"
                f"- p ≤ {adj_p_threshold}\n\n"
                f"**Results:**\n"
                f"- Total features analyzed: {len(rows)}\n"
                f"- {positive_label}: {n_sig_up}\n"
                f"- {negative_label}: {n_sig_down}\n"
                f"- Not significant: {n_not_sig}\n"
            )
            summary += f"\n**File saved to:** `{user_facing_path}`\n"

            result_items: list[types.Content] = [
                types.TextContent(type="text", text=summary, mimeType="text/markdown"),
            ]

            # Inline base64 image for direct display in the chat. AgentCore
            # Runtime allows 100 MB payloads; we cap at 50 MB defensively to
            # leave headroom for clients with stricter size limits.
            if len(png_bytes) < 50_000_000:
                img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
                result_items.append(
                    types.ImageContent(type="image", data=img_b64, mimeType="image/png")
                )
            else:
                logger.warning(
                    f"Volcano plot too large for inline ({len(png_bytes)} bytes); "
                    f"image not embedded inline"
                )

            # Save instructions: cross-client portable mechanism for getting
            # the inline PNG bytes onto the user's local filesystem when this
            # MCP server runs in a remote container. The block offers three
            # options: right-click save, LLM-client filesystem write, and a
            # multi-line Python script. See _make_save_instructions.
            #
            # Size-aware guard: emitting both the inline ImageContent AND the
            # save-instructions block doubles the PNG payload (once as
            # ImageContent base64, once inside the script). For plots over
            # ~350KB, this can push the total response past Claude Desktop's
            # ~1MB tool-result cap. In that case we emit a SHORTER guidance
            # text that points users to the inline image (right-click save)
            # rather than embedding the bytes a second time.
            suggested_filename = os.path.basename(user_facing_path)

            # Register the plot in the per-session "last plots" registry so the
            # user can later call `get_save_script(filename=...)` to retrieve a
            # full self-contained Python save script. This decouples the bulky
            # base64 payload from the routine plot response: the response stays
            # small (only the inline ImageContent), and the user pulls the
            # heavy script on demand when they actually want to save the file
            # outside of right-click-save.
            _register_plot(suggested_filename, png_bytes, user_facing_path)

            # Short save hint (~400 bytes) describing the three save options
            # WITHOUT duplicating the PNG bytes in the response. The previous
            # design embedded the full base64 script inline, doubling the
            # response size and burning conversation context ~2× faster than
            # necessary. The hint points users to right-click save, LLM-client
            # filesystem write, or the on-demand `get_save_script` tool.
            result_items.append(
                types.TextContent(
                    type="text",
                    text=_make_save_hint(user_facing_path, suggested_filename, len(png_bytes)),
                    mimeType="text/markdown",
                )
            )

            return result_items

        except Exception as e:
            logger.error(f"Error creating volcano plot: {e}")
            return [types.TextContent(type="text", text=f"Error creating volcano plot: {e}")]

    @mcp.tool(
        annotations={
            "title": "Create Venn Diagram (2-way or 3-way; cross-type supported)",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def create_venn_diagram(
        assay_id_1: str = Field(..., description="First assay identifier"),
        assay_id_2: str = Field(..., description="Second assay identifier"),
        assay_id_3: Optional[str] = Field(None, description="Third assay identifier (optional, for 3-way Venn diagram)"),
        data_type: str = Field("expression", description="Type of data to compare. Options: 'expression' (differentially expressed genes), 'methylation' (differentially methylated genes), 'abundance' (differentially abundant organisms), 'expression_methylation' (overlap between DE genes and DM genes across assays)"),
        log2fc_threshold: float = Field(1.0, description="Log2 fold change threshold for filtering genes (used for expression and abundance data types)"),
        methylation_diff_threshold: float = Field(0.0, description="Methylation difference threshold for filtering regions (used for methylation data type, default 0.0 means any change)"),
        adj_p_threshold: float = Field(0.05, description="Adjusted p-value threshold (default: 0.05). Applied to expression assays and to DESeq2 abundance rows."),
        q_value_threshold: float = Field(0.05, description="q-value threshold (default: 0.05). Applied to methylation assays and to ANCOM-BC abundance rows."),
        lnfc_threshold: Optional[float] = Field(None, description="Optional minimum |lnfc| magnitude for the 'abundance' data type. Only applied to rows with lnfc populated (ANCOM-BC). DESeq2 rows are not filtered by this parameter. Ignored for non-abundance data types. Leave unset (None) to skip lnfc filtering."),
        direction_pair: str = Field("all", description="For data_type='expression_methylation' only: which directional overlap(s) to render. 'all' (default) renders a 2x2 grid of all four biologically meaningful combinations: hypermethylated+downregulated (classical epigenetic silencing), hypomethylated+upregulated (loss of methylation enabling expression), hypermethylated+upregulated, and hypomethylated+downregulated. Specify one of 'hyper_down', 'hypo_up', 'hyper_up', 'hypo_down' to render only that single overlap. Ignored for non-expression_methylation data types."),
        in_promoter: Optional[bool] = Field(None, description="MethylationRegion location filter (data_type='methylation' or 'expression_methylation' only). When True, only include methylation regions overlapping a gene promoter. When False, exclude promoter-overlapping regions. None (default) imposes no promoter filter. Maps directly to the in_promoter property on MethylationRegion nodes."),
        in_exon: Optional[bool] = Field(None, description="MethylationRegion location filter (data_type='methylation' or 'expression_methylation' only). When True, only include methylation regions overlapping an exon. When False, exclude exon-overlapping regions. None (default) imposes no exon filter. Maps directly to the in_exon property on MethylationRegion nodes."),
        in_intron: Optional[bool] = Field(None, description="MethylationRegion location filter (data_type='methylation' or 'expression_methylation' only). When True, only include methylation regions overlapping an intron. When False, exclude intron-overlapping regions. None (default) imposes no intron filter. Maps directly to the in_intron property on MethylationRegion nodes."),
        dist_to_feature_max: Optional[int] = Field(None, description="MethylationRegion distance filter (data_type='methylation' or 'expression_methylation' only). When set, only include methylation regions whose dist_to_feature (distance in bp to the nearest gene feature) is <= this value. Useful for restricting to regions close to gene bodies. None (default) imposes no distance filter."),
        figsize_width: int = Field(10, description="Figure width in inches"),
        figsize_height: int = Field(6, description="Figure height in inches")
    ) -> list[types.Content]:
        """Create Venn diagrams comparing differential data between 2 or 3 assays.
        
        Supports multiple data types:
        - 'expression': Compares differentially expressed genes (upregulated/downregulated by log2fc)
        - 'methylation': Compares differentially methylated genes (hypermethylated/hypomethylated by methylation_diff)
        - 'abundance': Compares differentially abundant organisms (increased/decreased by log2fc; works for both DESeq2 and ANCOM-BC)
        - 'expression_methylation': Cross-comparison showing directional overlaps between differentially expressed genes 
          from one assay and differentially methylated genes from another assay (2-way only)
        
        Creates side-by-side Venn diagrams showing:
        - Left: Positive direction (upregulated / hypermethylated / increased abundance)
        - Right: Negative direction (downregulated / hypomethylated / decreased abundance)
        
        For 'expression_methylation' mode, this tool computes FOUR directional overlaps so the
        user can see which kind of expression–methylation coupling is happening. By default
        (direction_pair='all') it renders all four as a 2x2 grid:
          - Hypermethylated + Downregulated  (classical epigenetic silencing — promoter methylation reducing expression)
          - Hypomethylated + Upregulated     (loss of methylation enabling expression)
          - Hypermethylated + Upregulated    (less canonical; e.g. gene-body methylation activating expression)
          - Hypomethylated + Downregulated   (less canonical; non-classical)
        If the user names a specific pairing (e.g. "create a Venn of hypermethylated and
        downregulated genes"), pass direction_pair='hyper_down' to render just that one.
        The summary always reports the size of all four overlaps, regardless of which
        subset is rendered.

        Methylation region location filters (for data_type='methylation' and the methylation
        side of 'expression_methylation'):
        - in_promoter, in_exon, in_intron: each accepts True (include only regions where this
          flag is true), False (exclude regions where this flag is true), or None (no filter).
          Multiple filters combine with AND. For example, in_promoter=True alone restricts
          to promoter-overlapping regions; in_promoter=True with in_intron=False restricts
          to promoter regions that are NOT intronic.
        - dist_to_feature_max: when set, include only regions whose distance to the nearest
          gene feature is <= this value (in bp). Useful for narrowing to regions tightly
          associated with gene bodies. dist_to_feature on the MethylationRegion node is
          the bp distance from the region to the nearest annotated feature.
        These filters apply during the Cypher query — they reduce the set of methylation
        regions BEFORE the overlap computation, so each gene set is the genes that have at
        least one methylation region passing all the filters and the magnitude/significance
        criteria.

        Significance filtering:
        - 'expression' and 'expression_methylation' (expression side): adj_p_value <= adj_p_threshold (default 0.05).
        - 'methylation' and 'expression_methylation' (methylation side): q_value <= q_value_threshold (default 0.05).
        - 'abundance' is method-aware: DESeq2 rows are filtered by adj_p_threshold, ANCOM-BC rows by q_value_threshold.

        Magnitude filtering for 'abundance':
        - log2fc_threshold applies to every row (log2fc is populated for both methods).
        - lnfc_threshold (optional) adds an ANCOM-BC-specific |lnfc| filter; DESeq2 rows (lnfc IS NULL) pass through.

        Returns a link to the plot and summary statistics.
        """
        
        valid_types = ("expression", "methylation", "abundance", "expression_methylation")
        if data_type not in valid_types:
            return [types.TextContent(type="text", text=f"Error: data_type must be one of {valid_types}. Got: '{data_type}'")]

        valid_direction_pairs = ("all", "hyper_down", "hypo_up", "hyper_up", "hypo_down")
        if direction_pair not in valid_direction_pairs:
            return [types.TextContent(
                type="text",
                text=f"Error: direction_pair must be one of {valid_direction_pairs}. Got: '{direction_pair}'"
            )]
        
        try:
            # --- Define Cypher queries and labels based on data_type ---
            
            assay_info_query = """
            MATCH (a:Assay {identifier: $assay_id})
            RETURN a.factors_1 AS factors_1, a.factors_2 AS factors_2,
                   a.technology AS technology, a.measurement AS measurement,
                   a.differential_analysis_method AS analysis_method
            """
            
            if data_type == "expression":
                item_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (mg:MGene)
                WHERE (r.log2fc > $threshold OR r.log2fc < -$threshold)
                  AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
                RETURN mg.symbol AS item_id, r.log2fc AS value
                """
                positive_label = "Upregulated Genes"
                negative_label = "Downregulated Genes"
                positive_criterion = lambda v, t: v > t
                negative_criterion = lambda v, t: v < -t
                threshold_key = "log2fc"
                item_label = "genes"
                
            elif data_type == "methylation":
                # Build the MethylationRegion location/distance filter clause.
                # Each in_promoter/in_exon/in_intron is a tri-state (True/False/None);
                # only non-None values produce a clause. dist_to_feature_max, when
                # set, adds an upper bound on distance. Parameters are passed via
                # $name placeholders so they're properly escaped by Neo4j.
                mr_clauses: list[str] = []
                mr_params: dict[str, Any] = {}
                if in_promoter is not None:
                    mr_clauses.append("                  AND mr.in_promoter = $mr_in_promoter")
                    mr_params["mr_in_promoter"] = in_promoter
                if in_exon is not None:
                    mr_clauses.append("                  AND mr.in_exon = $mr_in_exon")
                    mr_params["mr_in_exon"] = in_exon
                if in_intron is not None:
                    mr_clauses.append("                  AND mr.in_intron = $mr_in_intron")
                    mr_params["mr_in_intron"] = in_intron
                if dist_to_feature_max is not None:
                    mr_clauses.append("                  AND mr.dist_to_feature IS NOT NULL AND mr.dist_to_feature <= $mr_dist_max")
                    mr_params["mr_dist_max"] = dist_to_feature_max
                mr_filter_block = ("\n" + "\n".join(mr_clauses)) if mr_clauses else ""

                item_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE (r.methylation_diff > $threshold OR r.methylation_diff < -$threshold)
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{mr_filter_block}
                RETURN mg.symbol AS item_id, r.methylation_diff AS value
                """
                positive_label = "Hypermethylated Genes"
                negative_label = "Hypomethylated Genes"
                positive_criterion = lambda v, t: v > t
                negative_criterion = lambda v, t: v < -t
                threshold_key = "methylation_diff"
                item_label = "genes"
                # Stash the MethylationRegion params so the per-assay caller below
                # can merge them into the item_query parameter dict.
                _methylation_mr_params = mr_params
                
            elif data_type == "abundance":
                # Conditionally include the lnfc magnitude clause. When lnfc_threshold is
                # None we omit it entirely; when set we require |lnfc| >= threshold on
                # rows where lnfc is populated, leaving DESeq2 rows (lnfc IS NULL) untouched.
                if lnfc_threshold is None:
                    abund_lnfc_clause = ""
                else:
                    abund_lnfc_clause = (
                        "                  AND (r.lnfc IS NULL OR abs(r.lnfc) >= $lnfc_threshold)\n"
                    )
                item_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->
                      (o:Organism)
                WHERE r.log2fc IS NOT NULL AND (r.log2fc > $threshold OR r.log2fc < -$threshold)
                  AND (
                    (r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold)
                    OR (r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold)
                  )
{abund_lnfc_clause}                RETURN o.name AS item_id, r.log2fc AS value
                """
                positive_label = "Increased Abundance"
                negative_label = "Decreased Abundance"
                positive_criterion = lambda v, t: v > t
                negative_criterion = lambda v, t: v < -t
                threshold_key = "log2fc"
                item_label = "organisms"
                
            elif data_type == "expression_methylation":
                # Cross-type: assay_id_1 = expression assay, assay_id_2 = methylation assay
                if assay_id_3:
                    return [types.TextContent(type="text", text="Error: 'expression_methylation' mode only supports 2-way comparison (assay_id_1 for expression, assay_id_2 for methylation). Do not provide assay_id_3.")]
                
                expr_query = """
                MATCH (a:Assay {identifier: $assay_id})
                      -[r:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->
                      (mg:MGene)
                WHERE (r.log2fc > $threshold OR r.log2fc < -$threshold)
                  AND r.adj_p_value IS NOT NULL AND r.adj_p_value <= $adj_p_threshold
                RETURN mg.symbol AS item_id, r.log2fc AS value
                """

                # Build the MethylationRegion location/distance filter clause for
                # the methylation side of this cross-comparison. Same tri-state
                # semantics as in the 'methylation' branch above; see comment
                # there for details. Parameters use distinct names (mr_*) so
                # they don't collide with $assay_id / $meth_threshold / $q_value_threshold.
                em_mr_clauses: list[str] = []
                em_mr_params: dict[str, Any] = {}
                if in_promoter is not None:
                    em_mr_clauses.append("                  AND mr.in_promoter = $mr_in_promoter")
                    em_mr_params["mr_in_promoter"] = in_promoter
                if in_exon is not None:
                    em_mr_clauses.append("                  AND mr.in_exon = $mr_in_exon")
                    em_mr_params["mr_in_exon"] = in_exon
                if in_intron is not None:
                    em_mr_clauses.append("                  AND mr.in_intron = $mr_in_intron")
                    em_mr_params["mr_in_intron"] = in_intron
                if dist_to_feature_max is not None:
                    em_mr_clauses.append("                  AND mr.dist_to_feature IS NOT NULL AND mr.dist_to_feature <= $mr_dist_max")
                    em_mr_params["mr_dist_max"] = dist_to_feature_max
                em_mr_filter_block = ("\n" + "\n".join(em_mr_clauses)) if em_mr_clauses else ""

                meth_query = f"""
                MATCH (a:Assay {{identifier: $assay_id}})
                      -[r:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->
                      (mr:MethylationRegion)
                      <-[:METHYLATED_IN_MGmMR]-(mg:MGene)
                WHERE (r.methylation_diff > $meth_threshold OR r.methylation_diff < -$meth_threshold)
                  AND r.q_value IS NOT NULL AND r.q_value <= $q_value_threshold{em_mr_filter_block}
                RETURN DISTINCT mg.symbol AS item_id, r.methylation_diff AS value
                """
            
            # Determine threshold value to use
            if data_type == "methylation":
                threshold_val = methylation_diff_threshold
            elif data_type in ("expression", "abundance"):
                threshold_val = log2fc_threshold
            # expression_methylation uses both thresholds separately
            
            # --- Fetch data from Neo4j ---
            # All queries here are independent reads on different assays / different
            # queries. Run them concurrently across separate sessions to collapse
            # what was up to 6 sequential RTTs into roughly 1 RTT-equivalent.
            async def _run(cypher, params):
                async with neo4j_driver.session(database=database, default_access_mode=READ_ACCESS) as s:
                    return await s.execute_read(_read, cypher, params)

            # Build the metadata-fetch task list (1 per assay)
            meta_tasks = [
                _run(assay_info_query, {"assay_id": assay_id_1}),
                _run(assay_info_query, {"assay_id": assay_id_2}),
            ]
            if assay_id_3:
                meta_tasks.append(_run(assay_info_query, {"assay_id": assay_id_3}))

            # Build the items-fetch task list
            if data_type == "expression_methylation":
                # Assay 1 = expression, Assay 2 = methylation. The methylation
                # query also receives any MethylationRegion location/distance
                # filter parameters that were set on the tool call. em_mr_params
                # is built in the expression_methylation branch above; it's an
                # empty dict if no MR filters were specified.
                meth_call_params = {
                    "assay_id": assay_id_2,
                    "threshold": log2fc_threshold,
                    "meth_threshold": methylation_diff_threshold,
                    "q_value_threshold": q_value_threshold,
                    **em_mr_params,
                }
                items_tasks = [
                    _run(expr_query, {"assay_id": assay_id_1, "threshold": log2fc_threshold, "adj_p_threshold": adj_p_threshold}),
                    _run(meth_query, meth_call_params),
                ]
            else:
                # Build per-data-type extra params for item_query
                if data_type == "expression":
                    item_params_extra = {"adj_p_threshold": adj_p_threshold}
                elif data_type == "methylation":
                    # For methylation, plumb the MethylationRegion filter params
                    # (in_promoter/in_exon/in_intron/dist_to_feature_max) through
                    # to every per-assay query. _methylation_mr_params was built
                    # in the methylation branch above; it's an empty dict if no
                    # MR filters were specified.
                    item_params_extra = {"q_value_threshold": q_value_threshold, **_methylation_mr_params}
                else:  # abundance — method-aware, needs both significance thresholds
                    item_params_extra = {"adj_p_threshold": adj_p_threshold, "q_value_threshold": q_value_threshold}
                    # lnfc_threshold is only referenced in the query string when set;
                    # include it in params only when the query actually uses it.
                    if lnfc_threshold is not None:
                        item_params_extra["lnfc_threshold"] = lnfc_threshold
                items_tasks = [
                    _run(item_query, {"assay_id": assay_id_1, "threshold": threshold_val, **item_params_extra}),
                    _run(item_query, {"assay_id": assay_id_2, "threshold": threshold_val, **item_params_extra}),
                ]
                if assay_id_3:
                    items_tasks.append(
                        _run(item_query, {"assay_id": assay_id_3, "threshold": threshold_val, **item_params_extra})
                    )

            # One big gather: every query runs concurrently
            all_results = await asyncio.gather(*meta_tasks, *items_tasks)

            # Slice the results back out in the original order
            n_meta = len(meta_tasks)
            meta_results = all_results[:n_meta]
            items_results = all_results[n_meta:]

            data1 = json.loads(meta_results[0])
            if not data1:
                return [types.TextContent(type="text", text=f"Error: Assay {assay_id_1} not found")]
            data2 = json.loads(meta_results[1])
            if not data2:
                return [types.TextContent(type="text", text=f"Error: Assay {assay_id_2} not found")]
            if assay_id_3:
                data3 = json.loads(meta_results[2])
                if not data3:
                    return [types.TextContent(type="text", text=f"Error: Assay {assay_id_3} not found")]

            items1_result = items_results[0]
            items2_result = items_results[1]
            if assay_id_3 and data_type != "expression_methylation":
                items3_result = items_results[2]
            
            # Parse items data
            items1_data = json.loads(items1_result)
            items2_data = json.loads(items2_result)
            if assay_id_3 and data_type != "expression_methylation":
                items3_data = json.loads(items3_result)
            
            # Extract assay metadata
            def _extract_info(d):
                f1 = d[0].get('factors_1', []) or []
                f2 = d[0].get('factors_2', []) or []
                tech = d[0].get('technology', 'N/A')
                meas = d[0].get('measurement', 'N/A')
                method = d[0].get('analysis_method', 'N/A')
                return ",".join(f1) if f1 else "N/A", ",".join(f2) if f2 else "N/A", tech, meas, method
            
            factors_1, factors_2, tech1, meas1, method1 = _extract_info(data1)
            factors_1_a2, factors_2_a2, tech2, meas2, method2 = _extract_info(data2)
            if assay_id_3 and data_type != "expression_methylation":
                factors_1_a3, factors_2_a3, tech3, meas3, method3 = _extract_info(data3)
            
            study = "-".join(assay_id_1.split("-")[:2])
            
            # --- Build sets ---
            if data_type == "expression_methylation":
                # Split DE genes into up/down and DM genes into hyper/hypo so we can
                # build the four directional overlaps. The Cypher already filtered
                # by |value| > threshold AND significance, so here we only need to
                # split on sign. Defensive None check on item_id covers the
                # OPTIONAL MATCH case where methylation regions could be unlinked.
                de_genes_up = set(g['item_id'] for g in items1_data
                                  if g['item_id'] and g['value'] > log2fc_threshold)
                de_genes_down = set(g['item_id'] for g in items1_data
                                    if g['item_id'] and g['value'] < -log2fc_threshold)
                dm_genes_hyper = set(g['item_id'] for g in items2_data
                                     if g['item_id'] and g['value'] > methylation_diff_threshold)
                dm_genes_hypo = set(g['item_id'] for g in items2_data
                                    if g['item_id'] and g['value'] < -methylation_diff_threshold)
                
            else:
                # Standard same-type comparison
                assay1_pos = set(g['item_id'] for g in items1_data if g['item_id'] and positive_criterion(g['value'], threshold_val))
                assay1_neg = set(g['item_id'] for g in items1_data if g['item_id'] and negative_criterion(g['value'], threshold_val))
                assay2_pos = set(g['item_id'] for g in items2_data if g['item_id'] and positive_criterion(g['value'], threshold_val))
                assay2_neg = set(g['item_id'] for g in items2_data if g['item_id'] and negative_criterion(g['value'], threshold_val))
                
                if assay_id_3:
                    assay3_pos = set(g['item_id'] for g in items3_data if g['item_id'] and positive_criterion(g['value'], threshold_val))
                    assay3_neg = set(g['item_id'] for g in items3_data if g['item_id'] and negative_criterion(g['value'], threshold_val))
            
            # --- Create plots ---
            matplotlib.use('Agg')
            
            # Colors
            assay1_color = '#ffb3b3'  # Light red
            assay2_color = '#b3d9ff'  # Light blue
            assay3_color = '#b3ffb3'  # Light green
            overlap_color = '#ff99cc'  # Red+blue overlap
            
            if data_type == "expression_methylation":
                # Directional expression–methylation overlap. We compute all four
                # biologically meaningful combinations regardless of what the user
                # asked to render, so the summary can report all four counts.
                pairings = [
                    ("hyper_down", "Hypermethylated + Downregulated", "Hypermethylated promoter genes", "Downregulated genes",
                     dm_genes_hyper, de_genes_down),
                    ("hypo_up",    "Hypomethylated + Upregulated",    "Hypomethylated promoter genes", "Upregulated genes",
                     dm_genes_hypo,  de_genes_up),
                    ("hyper_up",   "Hypermethylated + Upregulated",   "Hypermethylated promoter genes", "Upregulated genes",
                     dm_genes_hyper, de_genes_up),
                    ("hypo_down",  "Hypomethylated + Downregulated",  "Hypomethylated promoter genes", "Downregulated genes",
                     dm_genes_hypo,  de_genes_down),
                ]

                # Precompute the four overlap gene sets for the summary, keyed by
                # the direction_pair identifier so the summary section can always
                # report all four counts and gene lists regardless of which
                # subset was rendered.
                overlap_sets_by_key = {}
                for key, _, _, _, dm_set, de_set in pairings:
                    overlap_sets_by_key[key] = dm_set & de_set

                # Choose which pairings to actually render
                if direction_pair == "all":
                    render_pairings = pairings
                else:
                    render_pairings = [p for p in pairings if p[0] == direction_pair]

                # Layout: 2x2 grid for "all", single panel otherwise. We use
                # plt.subplots with explicit nrows/ncols so the single-pair case
                # is a 1x1 figure with the same code path.
                n_render = len(render_pairings)
                if n_render == 4:
                    fig, axes = plt.subplots(2, 2, figsize=(figsize_width, figsize_height))
                    axes_flat = axes.flatten()
                else:
                    fig, ax_single = plt.subplots(1, 1, figsize=(figsize_width // 2 + 2, figsize_height))
                    axes_flat = [ax_single]

                # Colors: methylation side blue, expression side red, overlap purple
                meth_color = '#b3d9ff'    # light blue
                expr_color = '#ffb3b3'    # light red
                overlap_color = '#cc99ff' # purple

                for ax_cur, (key, title, label_left, label_right, dm_set, de_set) in zip(axes_flat, render_pairings):
                    v = venn2([dm_set, de_set],
                              set_labels=(label_left, label_right),
                              ax=ax_cur)
                    if v.get_patch_by_id('10'):
                        v.get_patch_by_id('10').set_color(meth_color)
                        v.get_patch_by_id('10').set_alpha(0.7)
                    if v.get_patch_by_id('01'):
                        v.get_patch_by_id('01').set_color(expr_color)
                        v.get_patch_by_id('01').set_alpha(0.7)
                    if v.get_patch_by_id('11'):
                        v.get_patch_by_id('11').set_color(overlap_color)
                        v.get_patch_by_id('11').set_alpha(0.8)

                    for text in v.subset_labels:
                        if text:
                            text.set_fontsize(12 if n_render == 4 else 16)
                    for text in v.set_labels:
                        if text:
                            text.set_fontsize(9 if n_render == 4 else 12)
                            text.set_fontweight('bold')

                    ax_cur.set_title(title, fontsize=11 if n_render == 4 else 14,
                                     fontweight='bold', y=1.02)

                fig.suptitle(f'{study}: Expression–Methylation Directional Overlap',
                             fontsize=15, fontweight='bold', y=0.995)

                # Legend across the bottom — once for the whole figure, not per panel
                legend_y = 0.02
                legend_fontsize = 8 if n_render == 4 else 9
                legend_spacing = 0.025 if n_render == 4 else 0.045
                legend_x = 0.10

                fig.text(legend_x, legend_y + legend_spacing,
                        f'DE Assay: ({factors_1}) vs ({factors_2}) [{method1}]',
                        ha='left', fontsize=legend_fontsize, style='italic',
                        bbox=dict(boxstyle='round,pad=0.4', facecolor=expr_color, alpha=0.6, edgecolor='none'))
                fig.text(legend_x, legend_y,
                        f'DM Assay: ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]',
                        ha='left', fontsize=legend_fontsize, style='italic',
                        bbox=dict(boxstyle='round,pad=0.4', facecolor=meth_color, alpha=0.6, edgecolor='none'))

                plt.tight_layout(rect=[0, 0.10 if n_render == 4 else 0.14, 1, 0.94])
                
            else:
                # Standard side-by-side Venn (2 or 3 way)
                fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(figsize_width, figsize_height))
                
                # Build assay labels based on data type
                def _assay_label(tech, meas, method):
                    parts = []
                    if method and method != 'N/A':
                        parts.append(method)
                    elif meas and meas != 'N/A':
                        parts.append(meas)
                    return " ".join(parts) if parts else "Assay"
                
                if assay_id_3:
                    # 3-way Venn
                    color_map = {
                        '100': assay1_color,
                        '010': assay2_color,
                        '001': assay3_color,
                        '110': '#ff99cc',
                        '101': '#ffb3d9',
                        '011': '#99ffcc',
                        '111': '#e6b3ff',
                    }
                    
                    for ax_cur, items_list, title in [
                        (ax1, [assay1_pos, assay2_pos, assay3_pos], positive_label),
                        (ax2, [assay1_neg, assay2_neg, assay3_neg], negative_label)
                    ]:
                        v = venn3(items_list, set_labels=('', '', ''), ax=ax_cur)
                        for region_id, color in color_map.items():
                            patch = v.get_patch_by_id(region_id)
                            if patch:
                                patch.set_color(color)
                                patch.set_alpha(0.7)
                        for text in v.subset_labels:
                            if text:
                                text.set_fontsize(14)
                        
                        thresh_str = f"{threshold_key} > {threshold_val}" if "Upregulated" in title or "Hyper" in title or "Increased" in title else f"{threshold_key} < -{threshold_val}"
                        ax_cur.set_title(f'{title}\n({thresh_str})', fontsize=13, fontweight='bold', y=1.02)
                        
                        ax_cur.set_xlim(-1.0, 1.0)
                        ax_cur.set_ylim(-0.9, 0.9)
                        
                        fixed_bottom_y = -0.75
                        try:
                            if hasattr(v, 'circles') and v.circles and len(v.circles) >= 3:
                                c1_x, _ = v.circles[0].center
                                c2_x, _ = v.circles[1].center
                                c3_x, c3_y = v.circles[2].center
                                ax_cur.text(c1_x, fixed_bottom_y, 'Assay 1', ha='center', fontsize=12, fontweight='bold')
                                ax_cur.text(c2_x, fixed_bottom_y, 'Assay 2', ha='center', fontsize=12, fontweight='bold')
                                ax_cur.text(c3_x, c3_y + 0.5, 'Assay 3', ha='center', fontsize=12, fontweight='bold')
                            else:
                                ax_cur.text(-0.5, fixed_bottom_y, 'Assay 1', ha='center', fontsize=12, fontweight='bold')
                                ax_cur.text(0.5, fixed_bottom_y, 'Assay 2', ha='center', fontsize=12, fontweight='bold')
                                ax_cur.text(0, 0.5, 'Assay 3', ha='center', fontsize=12, fontweight='bold')
                        except Exception:
                            ax_cur.text(-0.5, fixed_bottom_y, 'Assay 1', ha='center', fontsize=12, fontweight='bold')
                            ax_cur.text(0.5, fixed_bottom_y, 'Assay 2', ha='center', fontsize=12, fontweight='bold')
                            ax_cur.text(0, 0.5, 'Assay 3', ha='center', fontsize=12, fontweight='bold')
                    
                    fig.suptitle(f'{study}', fontsize=22, fontweight='bold', y=0.98)
                    
                    legend_y = 0.01
                    legend_fontsize = 9
                    legend_spacing = 0.045
                    legend_x = 0.10
                    
                    lbl1 = f'Assay 1: ({factors_1}) vs ({factors_2}) [{method1}]'
                    lbl2 = f'Assay 2: ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]'
                    lbl3 = f'Assay 3: ({factors_1_a3}) vs ({factors_2_a3}) [{method3}]'
                    
                    fig.text(legend_x, legend_y + 2*legend_spacing, lbl1,
                            ha='left', fontsize=legend_fontsize, style='italic',
                            bbox=dict(boxstyle='round,pad=0.5', facecolor=assay1_color, alpha=0.6, edgecolor='none'))
                    fig.text(legend_x, legend_y + legend_spacing, lbl2,
                            ha='left', fontsize=legend_fontsize, style='italic',
                            bbox=dict(boxstyle='round,pad=0.5', facecolor=assay2_color, alpha=0.6, edgecolor='none'))
                    fig.text(legend_x, legend_y, lbl3,
                            ha='left', fontsize=legend_fontsize, style='italic',
                            bbox=dict(boxstyle='round,pad=0.5', facecolor=assay3_color, alpha=0.6, edgecolor='none'))
                    
                else:
                    # 2-way Venn
                    for ax_cur, set1, set2, title in [
                        (ax1, assay1_pos, assay2_pos, positive_label),
                        (ax2, assay1_neg, assay2_neg, negative_label)
                    ]:
                        v = venn2([set1, set2], set_labels=('', ''), ax=ax_cur)
                        
                        if v.get_patch_by_id('10'):
                            v.get_patch_by_id('10').set_color(assay1_color)
                            v.get_patch_by_id('10').set_alpha(0.7)
                        if v.get_patch_by_id('01'):
                            v.get_patch_by_id('01').set_color(assay2_color)
                            v.get_patch_by_id('01').set_alpha(0.7)
                        if v.get_patch_by_id('11'):
                            v.get_patch_by_id('11').set_color(overlap_color)
                            v.get_patch_by_id('11').set_alpha(0.7)
                        
                        for text in v.subset_labels:
                            if text:
                                text.set_fontsize(16)
                        
                        thresh_str = f"{threshold_key} > {threshold_val}" if "Upregulated" in title or "Hyper" in title or "Increased" in title else f"{threshold_key} < -{threshold_val}"
                        ax_cur.set_title(f'{title}\n({thresh_str})', fontsize=13, fontweight='bold', y=1.02)
                        
                        ax_cur.set_xlim(-0.75, 0.75)
                        ax_cur.set_ylim(-0.75, 0.75)
                        ax_cur.set_aspect('equal')
                        
                        ax_cur.text(-0.4, -0.6, 'Assay 1', ha='center', fontsize=14, fontweight='bold')
                        ax_cur.text(0.4, -0.6, 'Assay 2', ha='center', fontsize=14, fontweight='bold')
                    
                    fig.suptitle(f'{study}', fontsize=22, fontweight='bold', y=0.98)
                    
                    legend_y = 0.01
                    legend_fontsize = 10
                    legend_spacing = 0.045
                    legend_x = 0.15
                    
                    lbl1 = f'Assay 1: ({factors_1}) vs ({factors_2}) [{method1}]'
                    lbl2 = f'Assay 2: ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]'
                    
                    fig.text(legend_x, legend_y + legend_spacing, lbl1,
                            ha='left', fontsize=legend_fontsize, style='italic',
                            bbox=dict(boxstyle='round,pad=0.5', facecolor=assay1_color, alpha=0.6, edgecolor='none'))
                    fig.text(legend_x, legend_y, lbl2,
                            ha='left', fontsize=legend_fontsize, style='italic',
                            bbox=dict(boxstyle='round,pad=0.5', facecolor=assay2_color, alpha=0.6, edgecolor='none'))
                
                plt.tight_layout(rect=[0, 0.16, 1, 0.96])
            
            # --- Save the figure ---
            # Render to in-memory PNG bytes; downstream code uses them for
            # both the inline ImageContent and the on-demand save script.
            import io as _venn_io
            num_assays = 3 if (assay_id_3 and data_type != "expression_methylation") else 2
            png_buf = _venn_io.BytesIO()
            try:
                # dpi=120 matches the volcano tool to keep inline ImageContent
                # payloads small and uniform across plot types.
                plt.savefig(png_buf, format='png', dpi=120, bbox_inches='tight')
                png_buf.seek(0)
                venn_png_bytes = png_buf.getvalue()
                logger.info(f"SUCCESS: Venn rendered to memory ({len(venn_png_bytes)} bytes)")
            except Exception as render_err:
                logger.error(f"ERROR rendering Venn PNG: {render_err}")
                venn_png_bytes = None

            stem = f'venn_{data_type}_{num_assays}way_{study}'
            venn_write_path, venn_user_facing_path, venn_download_link = _resolve_output_paths(
                stem, extension="png"
            )

            if venn_png_bytes is not None and not _is_remote_deployment():
                # Only write to the server's filesystem in stdio/local mode
                # where the user can actually reach the file. In AgentCore /
                # remote deployments the inline ImageContent + on-demand
                # get_save_script is the user's reachable copy.
                try:
                    os.makedirs(os.path.dirname(venn_write_path), exist_ok=True)
                    with open(venn_write_path, "wb") as f:
                        f.write(venn_png_bytes)
                    logger.info(f"Venn saved to {venn_write_path} ({len(venn_png_bytes)} bytes)")
                except Exception as save_err:
                    logger.warning(f"Could not save Venn to {venn_write_path}: {save_err}")
                    # The PNG bytes are still in memory and will be returned
                    # inline + via the save command, so disk-write failure is
                    # not fatal.

            plt.close(fig)
            
            # --- Build summary ---
            if data_type == "expression_methylation":
                # Report all four directional overlap counts regardless of which
                # subset was rendered, so users always see the full picture.
                hyper_down_genes = sorted(overlap_sets_by_key["hyper_down"])
                hypo_up_genes    = sorted(overlap_sets_by_key["hypo_up"])
                hyper_up_genes   = sorted(overlap_sets_by_key["hyper_up"])
                hypo_down_genes  = sorted(overlap_sets_by_key["hypo_down"])

                if direction_pair == "all":
                    rendered_label = "All four directional overlaps (2×2 grid)"
                else:
                    pretty_names = {
                        "hyper_down": "Hypermethylated + Downregulated",
                        "hypo_up":    "Hypomethylated + Upregulated",
                        "hyper_up":   "Hypermethylated + Upregulated",
                        "hypo_down":  "Hypomethylated + Downregulated",
                    }
                    rendered_label = pretty_names[direction_pair]

                summary = f"""## Expression–Methylation Overlap Venn Diagram

**Study:** {study}
**Data type:** expression_methylation (cross-comparison)
**Rendered:** {rendered_label}

**Expression Assay (Assay 1):** ({factors_1}) vs ({factors_2}) [{method1}]
**Methylation Assay (Assay 2):** ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]

**Thresholds:** Log2FC ≥ ±{log2fc_threshold} (adj.p ≤ {adj_p_threshold}), Methylation Diff ≥ ±{methylation_diff_threshold} (q ≤ {q_value_threshold})

**File saved to:** {venn_user_facing_path}

### Set sizes
- Upregulated genes: {len(de_genes_up)}
- Downregulated genes: {len(de_genes_down)}
- Hypermethylated genes: {len(dm_genes_hyper)}
- Hypomethylated genes: {len(dm_genes_hypo)}

### Directional overlaps (all four computed)
| Pairing | Overlap size | Biological interpretation |
|---|---|---|
| Hypermethylated + Downregulated | **{len(hyper_down_genes)}** | Classical epigenetic silencing |
| Hypomethylated + Upregulated    | **{len(hypo_up_genes)}**    | Loss of methylation enabling expression |
| Hypermethylated + Upregulated   | **{len(hyper_up_genes)}**   | Less canonical (e.g. gene-body activation) |
| Hypomethylated + Downregulated  | **{len(hypo_down_genes)}**  | Less canonical / non-classical |
"""

                # Helper: list out gene symbols for one pairing, truncating to 50.
                def _gene_list_section(label, gene_list):
                    if not gene_list:
                        return f"\n### {label}\n_No genes in this overlap._\n"
                    if len(gene_list) <= 50:
                        return f"\n### {label} ({len(gene_list)})\n{', '.join(gene_list)}\n"
                    return (
                        f"\n### {label} (first 50 of {len(gene_list)})\n"
                        f"{', '.join(gene_list[:50])}\n"
                    )

                # When the user asked for one specific pairing, surface that pairing's
                # gene list prominently. When they asked for "all", surface all four
                # lists in succession so they have the full breakdown inline.
                if direction_pair == "all":
                    summary += _gene_list_section("Hypermethylated + Downregulated genes", hyper_down_genes)
                    summary += _gene_list_section("Hypomethylated + Upregulated genes",    hypo_up_genes)
                    summary += _gene_list_section("Hypermethylated + Upregulated genes",   hyper_up_genes)
                    summary += _gene_list_section("Hypomethylated + Downregulated genes",  hypo_down_genes)
                else:
                    pretty_names = {
                        "hyper_down": "Hypermethylated + Downregulated genes",
                        "hypo_up":    "Hypomethylated + Upregulated genes",
                        "hyper_up":   "Hypermethylated + Upregulated genes",
                        "hypo_down":  "Hypomethylated + Downregulated genes",
                    }
                    summary += _gene_list_section(
                        pretty_names[direction_pair],
                        sorted(overlap_sets_by_key[direction_pair]),
                    )
                    
            elif assay_id_3:
                # 3-way stats
                pos_only_1 = len(assay1_pos - assay2_pos - assay3_pos)
                pos_only_2 = len(assay2_pos - assay1_pos - assay3_pos)
                pos_only_3 = len(assay3_pos - assay1_pos - assay2_pos)
                pos_1_2 = len((assay1_pos & assay2_pos) - assay3_pos)
                pos_1_3 = len((assay1_pos & assay3_pos) - assay2_pos)
                pos_2_3 = len((assay2_pos & assay3_pos) - assay1_pos)
                pos_all = len(assay1_pos & assay2_pos & assay3_pos)
                
                neg_only_1 = len(assay1_neg - assay2_neg - assay3_neg)
                neg_only_2 = len(assay2_neg - assay1_neg - assay3_neg)
                neg_only_3 = len(assay3_neg - assay1_neg - assay2_neg)
                neg_1_2 = len((assay1_neg & assay2_neg) - assay3_neg)
                neg_1_3 = len((assay1_neg & assay3_neg) - assay2_neg)
                neg_2_3 = len((assay2_neg & assay3_neg) - assay1_neg)
                neg_all = len(assay1_neg & assay2_neg & assay3_neg)
                
                summary = f"""## 3-Way Venn Diagram ({data_type})

**Study:** {study}
**Data type:** {data_type}

**Assay 1:** ({factors_1}) vs ({factors_2}) [{method1}]
**Assay 2:** ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]
**Assay 3:** ({factors_1_a3}) vs ({factors_2_a3}) [{method3}]

**Threshold:** {threshold_key} ≥ ±{threshold_val}

**File saved to:** {venn_user_facing_path}

### {positive_label}:
- Assay 1 only: {pos_only_1} | Assay 2 only: {pos_only_2} | Assay 3 only: {pos_only_3}
- Assay 1 & 2: {pos_1_2} | Assay 1 & 3: {pos_1_3} | Assay 2 & 3: {pos_2_3}
- All three: {pos_all}
- Total: Assay 1={len(assay1_pos)}, Assay 2={len(assay2_pos)}, Assay 3={len(assay3_pos)}

### {negative_label}:
- Assay 1 only: {neg_only_1} | Assay 2 only: {neg_only_2} | Assay 3 only: {neg_only_3}
- Assay 1 & 2: {neg_1_2} | Assay 1 & 3: {neg_1_3} | Assay 2 & 3: {neg_2_3}
- All three: {neg_all}
- Total: Assay 1={len(assay1_neg)}, Assay 2={len(assay2_neg)}, Assay 3={len(assay3_neg)}
"""
            else:
                # 2-way stats
                pos_only_1 = len(assay1_pos - assay2_pos)
                pos_only_2 = len(assay2_pos - assay1_pos)
                pos_common = len(assay1_pos & assay2_pos)
                neg_only_1 = len(assay1_neg - assay2_neg)
                neg_only_2 = len(assay2_neg - assay1_neg)
                neg_common = len(assay1_neg & assay2_neg)
                
                summary = f"""## 2-Way Venn Diagram ({data_type})

**Study:** {study}
**Data type:** {data_type}

**Assay 1:** ({factors_1}) vs ({factors_2}) [{method1}]
**Assay 2:** ({factors_1_a2}) vs ({factors_2_a2}) [{method2}]

**Threshold:** {threshold_key} ≥ ±{threshold_val}

**File saved to:** {venn_user_facing_path}

### {positive_label}:
- Assay 1 only: {pos_only_1}
- Assay 2 only: {pos_only_2}
- Common: {pos_common}
- Total: Assay 1={len(assay1_pos)}, Assay 2={len(assay2_pos)}

### {negative_label}:
- Assay 1 only: {neg_only_1}
- Assay 2 only: {neg_only_2}
- Common: {neg_common}
- Total: Assay 1={len(assay1_neg)}, Assay 2={len(assay2_neg)}
"""
            
            # --- Build return ---
            venn_result_items: list[types.Content] = [
                types.TextContent(type="text", text=summary, mimeType="text/markdown"),
            ]

            # Inline base64 image from the in-memory bytes captured during save.
            # AgentCore Runtime supports 100 MB payloads; defensive 50 MB cap
            # protects against pathological cases on smaller-capacity clients.
            if venn_png_bytes is not None:
                if len(venn_png_bytes) < 50_000_000:
                    img_b64 = base64.standard_b64encode(venn_png_bytes).decode("utf-8")
                    venn_result_items.append(
                        types.ImageContent(type="image", data=img_b64, mimeType="image/png")
                    )
                else:
                    logger.warning(
                        f"Venn too large for inline ({len(venn_png_bytes)} bytes); "
                        f"image not embedded inline"
                    )

                # Save instructions: cross-client portable mechanism for
                # delivering the PNG bytes to the user's local filesystem in
                # remote deployments. See _make_save_instructions for full
                # rationale. Same pattern as the volcano tool: register the
                # plot bytes for on-demand save-script retrieval via
                # `get_save_script`, then emit only the short hint to keep the
                # response compact.
                venn_suggested_filename = os.path.basename(venn_user_facing_path)
                _register_plot(venn_suggested_filename, venn_png_bytes, venn_user_facing_path)
                venn_result_items.append(
                    types.TextContent(
                        type="text",
                        text=_make_save_hint(
                            venn_user_facing_path,
                            venn_suggested_filename,
                            len(venn_png_bytes),
                        ),
                        mimeType="text/markdown",
                    )
                )

            return venn_result_items
            
        except Exception as e:
            logger.error(f"Error creating Venn diagram: {e}")
            import traceback
            traceback.print_exc()
            return [types.TextContent(type="text", text=f"Error creating Venn diagram: {e}")]


    @mcp.tool(
        annotations={
            "title": "Clean Mermaid Class Diagram",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def clean_mermaid_diagram(mermaid_content: str) -> list[types.TextContent]:
        """Clean a Mermaid class diagram by removing unwanted elements.
        
        This tool removes:
        - All note statements that would render as unreadable yellow boxes
        - Empty curly braces from class definitions (handles both single-line and multi-line)
        - Strings after newline characters (e.g., truncates "ClassName\nextra" to "ClassName")
        
        Args:
            mermaid_content: The raw Mermaid class diagram content
            
        Returns:
            Cleaned Mermaid content with note statements, empty braces, and post-newline strings removed
        """
        # First, truncate any strings after \n characters in the entire content
        # This handles cases like "MEASURED_DIFFERENTIAL_METHYLATION_ASmMR\nmethylation_diff, q_value"
        mermaid_content = re.sub(r'(\S+)\\n[^\s\n]*', r'\1', mermaid_content)
        
        lines = mermaid_content.split('\n')
        cleaned_lines = []
        i = 0
        
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            # Remove vertical bars, they are not allowed in class diagrams
            stripped = stripped.replace('|', ' ')
            
            # Skip any line containing note syntax
            if (stripped.startswith('note ') or 
                'note for' in stripped or 
                'note left' in stripped or 
                'note right' in stripped):
                i += 1
                continue
            
            # Check for empty class definitions (single-line format)
            # Match patterns like: "class ClassName {     }" or "class ClassName { }"
            if re.match(r'^\s*class\s+\w+\s*\{\s*\}\s*$', line):
                # Replace the line with just the class name without braces
                line = re.sub(r'^(\s*class\s+\w+)\s*\{\s*\}\s*$', r'\1', line)
                cleaned_lines.append(line)
                i += 1
                continue
            
            # Check for empty class definitions (multi-line format)
            # Match: "class ClassName {" followed by "}" on next line(s)
            if re.match(r'^\s*class\s+\w+\s*\{\s*$', line):
                # Look ahead to check if next non-empty line is just "}"
                j = i + 1
                found_closing = False
                has_content = False
                
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line:  # Empty line, skip
                        j += 1
                        continue
                    if next_line == '}':  # Found closing brace
                        found_closing = True
                        break
                    else:  # Found content between braces
                        has_content = True
                        break
                
                if found_closing and not has_content:
                    # This is an empty class definition - remove the braces
                    class_match = re.match(r'^(\s*class\s+\w+)\s*\{\s*$', line)
                    if class_match:
                        cleaned_lines.append(class_match.group(1))
                    # Skip ahead past the closing brace
                    i = j + 1
                    continue
            
            cleaned_lines.append(line)
            i += 1
        
        cleaned_content = '\n'.join(cleaned_lines)
        return [types.TextContent(type="text", text=cleaned_content)]

    @mcp.tool(
        annotations={
            "title": "Create Chat Transcript (prompt only)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def create_chat_transcript() -> list[types.TextContent]:
        """Prompt for creating a chat transcript in markdown format with user prompts and Claude responses."""
        today = datetime.now().strftime("%Y-%m-%d")
    
        prompt = f"""Create a chat transcript in .md format following the outline below. 
1. Include prompts, text responses, and visualizations preferably inline, and when not possible as a link to a document. 
2. Include mermaid diagrams inline. Do not link to the mermaid file.
3. Do not include the prompt to create this transcript.
4. For plots (volcano plots, Venn diagrams, etc.): embed them as markdown image references using the file path returned by the tool, e.g.: ![Volcano Plot](/mnt/user-data/outputs/volcano_plot_....png). Do NOT skip or omit plots - they must appear inline in the transcript at the point in the conversation where they were generated.
5. Save the transcript to ~/Downloads/<descriptive-filename>.md

## Chat Transcript
<Title>

👤 **User**  
<prompt>

---

🧠 **Assistant**  
<entire text response goes here>

<!-- For each plot generated during the conversation, include the image inline like this:
![Plot Title](/mnt/user-data/outputs/<plot_filename>.png)
-->


*Created by [mcp-genelab](https://github.com/sbl-sdsc/mcp-genelab) {__version__} on {today}*

IMPORTANT: 
- After the footer above, add a line with the model string you are using).
- Save the complete transcript to ~/Downloads/ with a descriptive filename (e.g., ~/Downloads/filename-chat-transcript-{today}.md)
- Use the present_files tool to share the transcript file with the user.
- ALSO call present_files for EACH plot image (.png) generated during the conversation (volcano plots, Venn diagrams, etc.).
  You MUST do this for every single image file that was created. Look through the entire conversation history for any file paths ending in .png.
  Call present_files once with a list of ALL file paths (transcript + all images).
- Do NOT skip sharing the image files. The user needs both the transcript AND the image files.
"""
        return [types.TextContent(type="text", text=prompt)]

    @mcp.tool(
        annotations={
            "title": "Visualize KG Schema (prompt only)",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def visualize_schema() -> list[types.TextContent]:
        """Prompt for visualizing the knowledge graph schema using a Mermaid class diagram."""
        prompt = """Visualize the knowledge graph schema using a Mermaid class diagram. 

CRITICAL WORKFLOW - Follow these steps EXACTLY IN ORDER:

STEP 1-5: Generate Draft Diagram
1. First call get_schema() if it has not been called to retrieve the classes and predicates
2. Analyze the schema to identify:
   - Node classes (entities like Gene, Study, Assay, etc.)
   - Edge predicates (relationships between nodes)
   - Edge properties (predicates that describe data types like float, int, string, boolean, date, etc.)
3. Generate the raw Mermaid class diagram showing:
   - All node classes with their properties
   - For edges WITHOUT properties: show as labeled arrows between classes (e.g., `Mission --> Study : CONDUCTED_MIcS`)
   - For edges WITH properties: represent the edge as an intermediary class containing the properties, with unlabeled arrows connecting source → edge class → target
4. Make the diagram taller / less wide:
   - Set the diagram direction to TB (top→bottom): `direction TB`
5. Do not append newline characters

⚠️  STEP 6-9: MANDATORY CLEANING - CANNOT BE SKIPPED ⚠️
6. STOP HERE! You now have a draft diagram. DO NOT use it yet.
7. Call clean_mermaid_diagram and pass your draft diagram as the parameter
8. Wait for the tool to return the cleaned diagram
9. Your draft is now OBSOLETE. Delete it from your mind. You will use ONLY the cleaned output.

STEP 10-13: Present ONLY the Cleaned Diagram
10. Copy the EXACT text returned by clean_mermaid_diagram (not your draft)
11. Present this CLEANED diagram inline in a mermaid code block
12. Create a .mermaid file with ONLY the CLEANED diagram code (no markdown fences)
13. Save to ~/Downloads/<kg_name>-schema.mermaid and call present_files

⛔ STOP AND CHECK - Before you respond to the user:
□ Did I call clean_mermaid_diagram? If NO → Go back and call it now
□ Am I using the cleaned output? If NO → Replace with cleaned output
□ Does my diagram contain empty {} braces? If YES → You're using your draft, use cleaned output
□ Did I call present_files? If NO → Call it now

EDGES WITH PROPERTIES - CRITICAL GUIDELINES:
- When an edge predicate has associated properties (e.g., log2fc, adj_p_value), DO NOT use a separate namespace
- Instead, represent the edge as an intermediary class with the original predicate name
- Connect the source class to the edge class, then the edge class to the target class
- Example: Instead of `Assay --> Gene : MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG` with a separate EdgeProperties namespace,
  create:
    class MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG {
        float log2fc
        float adj_p_value
    }
    Assay --> MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG
    MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG --> MGene
- This approach clearly shows that the properties belong to the relationship itself

RENDERING REQUIREMENTS:
- The .mermaid file MUST contain ONLY the Mermaid diagram code
- DO NOT include markdown code fences (```mermaid) in the .mermaid file
- DO NOT include any explanatory text in the .mermaid file
- The file should start with "classDiagram" and contain only the diagram definition
- ALWAYS use present_files to share the .mermaid file after creating it

❌ COMMON MISTAKES - These will cause errors:
- Using your draft diagram instead of the cleaned output from clean_mermaid_diagram
- Not calling clean_mermaid_diagram at all
- Calling clean_mermaid_diagram but then using your original draft anyway
- Including empty curly braces {} for classes without properties (the cleaner removes these)
- Not calling present_files to share the final .mermaid file
- Using a separate EdgeProperties namespace instead of intermediary classes
"""
        return [types.TextContent(type="text", text=prompt)]

    
    
    return mcp


async def async_main() -> None:
    db_url = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "neo4jdemo")
    database = os.getenv("NEO4J_DATABASE", "spoke-genelab-v0.3.1")
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    # The server-level `instructions` string is surfaced by most MCP clients
    # (Claude Desktop, Claude.ai, Cursor, Cline, etc.) as system-prompt-level
    # guidance the LLM sees BEFORE deciding which tool to call. Use it to set a
    # global tool-selection policy so the specialist tools win over the
    # general-purpose `query` tool for the categories they cover. Without this,
    # clients will often reach for `query` because its docstring promises raw
    # Cypher access while the specialists' docstrings promise narrower outputs.
    DEFAULT_INSTRUCTIONS = (
        "Query the GeneLab KG for NASA spaceflight omics datasets: differential "
        "gene expression (RNA-Seq), DNA methylation (WGBS/methylKit), and microbial "
        "abundance (DESeq2/ANCOM-BC).\n"
        "\n"
        "TOOL SELECTION POLICY (must be followed):\n"
        "- For 'up/down-regulated genes', 'DEGs', 'differential expression', or any\n"
        "  per-assay log2fc question: ALWAYS call find_differentially_expressed_genes;\n"
        "  NEVER call the `query` tool for this.\n"
        "- For 'hyper/hypo-methylated regions', 'DMRs', methylation_diff rankings,\n"
        "  promoter/exon/intron-filtered methylation queries, or pooling methylation\n"
        "  across replicate assays: ALWAYS call find_differentially_methylated_regions;\n"
        "  NEVER call the `query` tool for this.\n"
        "- For 'differentially abundant organisms' or DESeq2/ANCOM-BC abundance:\n"
        "  ALWAYS call find_differentially_abundant_organisms; NEVER call `query`.\n"
        "- For study metadata, factor space, or 'what assays does study X have':\n"
        "  ALWAYS call get_study_info or select_assays; NEVER call `query`.\n"
        "- For comparing DEGs/DMRs/DA organisms across multiple assays: use the\n"
        "  matching find_common_* tool, NEVER the `query` tool.\n"
        "- For DE genes overlapping DM regions (expression-methylation coupling):\n"
        "  use find_common_de_genes_overlapping_dm_regions, NEVER `query`.\n"
        "\n"
        "The `query` tool is a fallback for ad-hoc Cypher only. The specialist tools\n"
        "additionally return formatted markdown tables and inline CSVs that `query`\n"
        "does not produce; falling back to `query` for the categories above loses\n"
        "both the formatting and the CSV side-channel and reproduces work the\n"
        "specialists already do correctly (parameter validation, parallel queries,\n"
        "method-aware significance filtering, region-vs-gene aggregation, etc.)."
    )
    instructions = os.getenv("INSTRUCTIONS", DEFAULT_INSTRUCTIONS)

    logger.info(f"Starting mcp-genelab server (transport={transport})")
    logger.info(f"Neo4j: {db_url}, database: {database}")
    logger.info("All Neo4j sessions use READ_ACCESS mode (write operations are blocked)")

    neo4j_driver = AsyncGraphDatabase.driver(
        db_url,
        auth=(
            username,
            password,
        ),
    )

    mcp = create_mcp_server(neo4j_driver, database, instructions, host=host, port=port)

    match transport:
        case "stdio":
            await mcp.run_stdio_async()
        case "sse":
            await mcp.run_sse_async()
        case "streamable-http" | "http":
            await mcp.run_streamable_http_async()
        case _:
            raise ValueError(f"Invalid transport: {transport} | Must be 'stdio', 'sse', 'streamable-http', or 'http'")


def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()

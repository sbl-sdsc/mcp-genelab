"""Plot output regression tests.

These guard the resource-based plot delivery architecture:

  1. Save instructions are always small (~1-2 KB) regardless of PNG size,
     because PNG bytes are no longer embedded in the response.
  2. Save instructions reference the plot:// resource URI and the
     fetch_plot tool, giving the client a retry-safe path to the bytes
     that is independent of the tool response.
  3. Plots are exposed as MCP resources under the plot:// URI scheme,
     and the resource_templates list advertises this correctly.
  4. Both plot tools render at the same conservative dpi.

The old tests in this file (size-guarded base64 embedding) are gone
because the architecture they pinned no longer exists. Bytes are now
delivered exclusively through plot:// resource fetches and fetch_plot
tool calls, never through inline base64 in tool response text."""
from __future__ import annotations

import asyncio
import re

import pytest


# --- Save-instruction invariants ----------------------------------------

def test_make_save_instructions_is_compact_for_small_png(server_module):
    """Even small PNGs no longer get base64 embedded in the response —
    the save instructions are pure markdown referencing the resource URI."""
    result = server_module._make_save_instructions(
        user_facing_path="/tmp/foo.png",
        suggested_filename="foo.png",
        png_size_bytes=50_000,  # 50 KB
    )
    assert len(result) < 3_000, (
        f"Save instructions for a small PNG should be under 3 KB; "
        f"got {len(result)} bytes. The resource-based architecture "
        f"keeps the response compact regardless of PNG size."
    )


def test_make_save_instructions_is_compact_for_huge_png(server_module):
    """The critical invariant: response size is independent of PNG size.
    A 5 MB PNG produces the same length of save instructions as a 50 KB
    PNG, because the bytes live in the registry, not the response."""
    result_small = server_module._make_save_instructions(
        user_facing_path="/tmp/foo.png",
        suggested_filename="foo.png",
        png_size_bytes=50_000,
    )
    result_huge = server_module._make_save_instructions(
        user_facing_path="/tmp/foo.png",
        suggested_filename="foo.png",
        png_size_bytes=5_000_000,
    )

    # The only difference between the two responses is the displayed size,
    # which differs by a handful of bytes ("48 KB" vs "4882 KB"). Lengths
    # should be within a few bytes.
    assert abs(len(result_huge) - len(result_small)) < 50, (
        f"Save-instruction length should be near-identical for small vs "
        f"huge PNGs (the bytes aren't in the response). Got "
        f"small={len(result_small)}, huge={len(result_huge)}."
    )
    assert len(result_huge) < 3_000, (
        f"Save instructions for any PNG size should be under 3 KB; "
        f"5 MB PNG produced {len(result_huge)} bytes."
    )


def test_make_save_instructions_does_not_embed_base64(server_module):
    """Hard-line guarantee: under no circumstances does the response
    contain a chunked base64 block. The simplest way to assert this is
    to confirm there's no large run of base64-alphabet characters."""
    result = server_module._make_save_instructions(
        user_facing_path="/tmp/foo.png",
        suggested_filename="foo.png",
        png_size_bytes=200_000,
    )
    # A chunked base64 payload would contain runs of base64-alphabet
    # characters separated only by newlines. Look for any contiguous
    # run of at least 200 base64-alphabet characters (line breaks are
    # 76-char by convention; >200 chars of pure base64 would span at
    # least 3 lines if newlines weren't there, which is much more than
    # any incidental token like a long filename).
    base64_run = re.compile(r"[A-Za-z0-9+/=]{200,}")
    match = base64_run.search(result)
    assert match is None, (
        f"Save instructions must not contain embedded base64; "
        f"found {len(match.group())} consecutive base64-alphabet chars."
    )


def test_make_save_instructions_references_plot_resource_uri(server_module):
    """The save instructions must point the user/client at the
    plot://<filename> resource URI as the canonical retrieval path."""
    result = server_module._make_save_instructions(
        user_facing_path="/tmp/venn.png",
        suggested_filename="venn_OSD-244.png",
        png_size_bytes=80_000,
    )
    assert "plot://venn_OSD-244.png" in result, (
        "Save instructions must reference the canonical plot:// resource URI."
    )


def test_make_save_instructions_references_fetch_plot_tool(server_module):
    """The save instructions must also point at the fetch_plot tool as a
    portable alternative for clients that don't render resources well."""
    result = server_module._make_save_instructions(
        user_facing_path="/tmp/venn.png",
        suggested_filename="venn_OSD-244.png",
        png_size_bytes=80_000,
    )
    assert "fetch_plot" in result, (
        "Save instructions must reference the fetch_plot tool as a "
        "portable retrieval path."
    )


# --- Plot resource exposure ---------------------------------------------

def test_plot_resource_template_is_registered(mcp_server):
    """The plot:// URI template must be registered as a resource template
    that MCP clients can discover via resources/templates/list."""
    templates = asyncio.run(mcp_server.list_resource_templates())
    plot_templates = [
        t for t in templates
        if t.uriTemplate == "plot://{filename}"
    ]
    assert plot_templates, (
        f"plot://{{filename}} resource template must be registered. "
        f"Got templates: {[t.uriTemplate for t in templates]}"
    )
    t = plot_templates[0]
    assert t.mimeType == "image/png", (
        f"plot:// resource template must declare mimeType=image/png; "
        f"got {t.mimeType!r}."
    )


def test_plot_resource_fetches_registered_png(mcp_server, server_module):
    """Registering a plot in _LAST_PLOTS must make it fetchable via the
    plot:// resource. This is the contract that lets fetch_plot and
    resources/read deliver canonical bytes without re-rendering."""
    # Register a synthetic PNG directly into the module-level registry.
    fake_png = b"\x89PNG\r\n\x1a\n" + b"abc" * 100  # 308 bytes
    filename = "test_resource_plot.png"
    server_module._register_plot(filename, fake_png, "/tmp/test.png")

    try:
        content = asyncio.run(mcp_server.read_resource(f"plot://{filename}"))
        # FastMCP returns a list of ReadResourceContents
        assert content, "read_resource returned no content"

        # Extract the bytes; FastMCP's ReadResourceContents has a `content`
        # attribute holding either bytes or str.
        retrieved_bytes = None
        for c in content:
            if hasattr(c, "content") and isinstance(c.content, bytes):
                retrieved_bytes = c.content
                break
        assert retrieved_bytes is not None, (
            f"Could not extract bytes from read_resource result: {content}"
        )
        assert retrieved_bytes == fake_png, (
            "Bytes returned via plot:// resource must match the registered "
            "PNG byte-for-byte."
        )
    finally:
        # Clean up the registry so other tests aren't affected.
        if filename in server_module._LAST_PLOTS:
            del server_module._LAST_PLOTS[filename]


def test_plot_resource_raises_for_unknown_filename(mcp_server, server_module):
    """Fetching a plot that's not in the registry must produce a clear
    error rather than silently returning empty bytes."""
    # Make sure the registry doesn't already have this name.
    if "nonexistent_plot.png" in server_module._LAST_PLOTS:
        del server_module._LAST_PLOTS["nonexistent_plot.png"]

    with pytest.raises(Exception) as exc_info:
        asyncio.run(mcp_server.read_resource(
            "plot://nonexistent_plot.png"
        ))
    # The error message should name the missing file so a maintainer
    # debugging a failed fetch can immediately see what went wrong.
    error_str = str(exc_info.value)
    assert "nonexistent_plot.png" in error_str, (
        f"Error message for missing plot must name the filename; "
        f"got: {error_str!r}"
    )


# --- fetch_plot tool behavior -------------------------------------------

def test_fetch_plot_returns_embedded_resource_for_registered_png(
    mcp_server, server_module,
):
    """fetch_plot must return the canonical bytes as an EmbeddedResource
    so clients can render them inline using the resource UI."""
    fake_png = b"\x89PNG\r\n\x1a\n" + b"xyz" * 50  # 158 bytes
    filename = "test_fetch_plot.png"
    server_module._register_plot(filename, fake_png, "/tmp/fp.png")

    try:
        result = asyncio.run(
            mcp_server.call_tool("fetch_plot", {"filename": filename})
        )
        content = result[0] if isinstance(result, tuple) else result

        # Look for an EmbeddedResource carrying the bytes.
        from mcp import types as mcp_types
        embedded = [
            c for c in content
            if isinstance(c, mcp_types.EmbeddedResource)
        ]
        assert embedded, (
            "fetch_plot must return an EmbeddedResource carrying the bytes. "
            f"Got content types: {[type(c).__name__ for c in content]}"
        )
        # Verify the bytes match by decoding the embedded base64.
        import base64
        retrieved = base64.b64decode(embedded[0].resource.blob)
        assert retrieved == fake_png, (
            "Bytes returned via fetch_plot must match the registered PNG."
        )
        # The URI should also be the canonical plot:// URI.
        assert str(embedded[0].resource.uri) == f"plot://{filename}", (
            f"EmbeddedResource URI should be plot://{filename}; "
            f"got {embedded[0].resource.uri!r}"
        )
    finally:
        if filename in server_module._LAST_PLOTS:
            del server_module._LAST_PLOTS[filename]


def test_fetch_plot_returns_helpful_error_for_unknown_filename(
    mcp_server, server_module,
):
    """fetch_plot must produce a helpful error message when called for a
    plot not in the registry, naming the missing file and listing what
    IS available."""
    if "ghost_plot.png" in server_module._LAST_PLOTS:
        del server_module._LAST_PLOTS["ghost_plot.png"]

    result = asyncio.run(
        mcp_server.call_tool("fetch_plot", {"filename": "ghost_plot.png"})
    )
    content = result[0] if isinstance(result, tuple) else result
    text = "\n".join(c.text for c in content if hasattr(c, "text"))
    assert "ghost_plot.png" in text, (
        "fetch_plot error must name the missing filename."
    )
    # The response should signal both (a) what went wrong — the registry
    # context — and (b) how to recover. We check for any of the recovery
    # signals so the test isn't tied to one specific phrasing.
    recovery_markers = [
        "Available plots",          # listing what IS in the registry
        "Re-run",                   # telling the user how to regenerate
        "regenerate",
        "create_volcano_plot",      # naming the recovery tools directly
        "create_venn_diagram",
    ]
    assert "registry" in text and any(m in text for m in recovery_markers), (
        f"fetch_plot error must explain what happened (mention 'registry') "
        f"AND how to recover (one of {recovery_markers!r}). "
        f"Got: {text!r}"
    )


# --- DPI invariants (carried over from the old fix) ---------------------

def test_venn_and_volcano_use_same_dpi(server_module):
    """Both plot tools render at the same conservative dpi. Carried over
    from the previous fix because keeping payload sizes uniform across
    plot types is still a good idea even with the resource layer."""
    import inspect
    src = inspect.getsource(server_module.create_mcp_server)
    dpi_values = re.findall(r"plt\.savefig\([^)]*dpi\s*=\s*(\d+)", src)
    assert len(dpi_values) >= 2, (
        f"Expected at least 2 plt.savefig(..., dpi=N) calls "
        f"(volcano and venn). Found dpi values: {dpi_values}"
    )
    unique_dpi = set(dpi_values)
    assert len(unique_dpi) == 1, (
        f"Volcano and Venn should render at the same dpi to keep "
        f"PNG sizes uniform. Found mixed dpi values: {dpi_values}."
    )


def test_plot_dpi_is_conservative(server_module):
    """Keep dpi <= 120. Higher dpi increases PNG size, which is fine for
    the resource layer (no inline base64), but inline ImageContent in the
    plot tool's own response still benefits from being smaller."""
    import inspect
    src = inspect.getsource(server_module.create_mcp_server)
    dpi_values = [
        int(d) for d in
        re.findall(r"plt\.savefig\([^)]*dpi\s*=\s*(\d+)", src)
    ]
    for dpi in dpi_values:
        assert dpi <= 120, (
            f"Plot dpi must be <= 120 to keep inline ImageContent "
            f"payloads small. Found dpi={dpi}."
        )

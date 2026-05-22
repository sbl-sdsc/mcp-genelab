"""Tools-list inspection tests.

What this file guards:
  - All 21 expected tools register correctly with FastMCP.
  - Every tool has a `title` annotation (human-readable, shown by clients).
  - Every tool declares readOnlyHint, idempotentHint, and openWorldHint —
    the three behavioral hints from the MCP spec that clients use for
    safety prompts and UI categorization.
  - The annotation TITLES are unique across the suite (a duplicate would
    make two tools indistinguishable in client UIs).
  - The `query` tool's title flags it as fallback.

If a future change removes annotations (e.g. someone reverts to bare
`@mcp.tool()`) or adds a tool without annotating it, these tests fail
immediately."""
from __future__ import annotations


EXPECTED_TOOLS = {
    "get_neo4j_schema",
    "set_output_directory",
    "get_output_directory",
    "get_save_script",
    "fetch_plot",
    "query",
    "get_node_metadata",
    "get_relationship_metadata",
    "get_study_info",
    "select_assays",
    "find_differentially_expressed_genes",
    "find_differentially_methylated_regions",
    "find_differentially_abundant_organisms",
    "find_common_differentially_expressed_genes",
    "find_common_differentially_methylated_regions",
    "find_common_differentially_abundant_organisms",
    "find_common_de_genes_overlapping_dm_regions",
    "create_volcano_plot",
    "create_venn_diagram",
    "clean_mermaid_diagram",
    "create_chat_transcript",
    "visualize_schema",
}


def test_expected_tool_count(tools_list):
    """21 tools are expected. A higher number means an undocumented tool was
    added; a lower number means one was removed or failed to register."""
    assert len(tools_list) == len(EXPECTED_TOOLS), (
        f"Expected {len(EXPECTED_TOOLS)} tools, got {len(tools_list)}: "
        f"{sorted(t.name for t in tools_list)}"
    )


def test_expected_tool_names_present(tools_list):
    """Every tool we expect by name is actually registered."""
    actual = {t.name for t in tools_list}
    missing = EXPECTED_TOOLS - actual
    extra = actual - EXPECTED_TOOLS
    assert not missing, f"Missing tools: {missing}"
    assert not extra, f"Unexpected tools: {extra}"


def test_all_tools_have_annotations(tools_list):
    """No bare @mcp.tool() decorators allowed — every tool must have an
    `annotations` object on its Tool descriptor."""
    missing = [t.name for t in tools_list if t.annotations is None]
    assert not missing, f"Tools with no annotations: {missing}"


def test_all_tools_have_title(tools_list):
    """Title is the user-visible label MCP clients use in their UI. We require
    a non-empty title on every tool so the catalog never has a blank entry."""
    missing = [
        t.name for t in tools_list
        if not getattr(t.annotations, "title", None)
    ]
    assert not missing, f"Tools missing title: {missing}"


def test_all_tools_declare_read_only_hint(tools_list):
    """readOnlyHint is a tri-state in the spec (True/False/None). We require
    a concrete True or False on every tool so clients can confidently route
    to safety-prompt UI or not."""
    missing = [
        t.name for t in tools_list
        if getattr(t.annotations, "readOnlyHint", None) is None
    ]
    assert not missing, f"Tools missing readOnlyHint: {missing}"


def test_all_tools_declare_idempotent_hint(tools_list):
    """idempotentHint signals whether repeated calls with identical args
    produce identical side effects. Every tool must declare it explicitly."""
    missing = [
        t.name for t in tools_list
        if getattr(t.annotations, "idempotentHint", None) is None
    ]
    assert not missing, f"Tools missing idempotentHint: {missing}"


def test_all_tools_declare_open_world_hint(tools_list):
    """openWorldHint signals whether the tool talks to external systems
    outside the server's control. For this server, NOTHING should have
    openWorldHint=True — we only touch one local Neo4j."""
    missing = [
        t.name for t in tools_list
        if getattr(t.annotations, "openWorldHint", None) is None
    ]
    assert not missing, f"Tools missing openWorldHint: {missing}"

    # Stronger guarantee: no tool actually reaches an external service.
    open_world = [
        t.name for t in tools_list if t.annotations.openWorldHint is True
    ]
    assert not open_world, (
        f"Tools claiming openWorldHint=True (this server only reads local "
        f"Neo4j; none should): {open_world}"
    )


def test_all_titles_are_unique(tools_list):
    """Two tools sharing a title would be indistinguishable in client UIs."""
    titles = [t.annotations.title for t in tools_list]
    assert len(set(titles)) == len(titles), (
        f"Duplicate titles found: "
        f"{[t for t in titles if titles.count(t) > 1]}"
    )


def test_query_tool_title_flags_fallback(tools_list):
    """The `query` tool's title must signal its fallback status to any client
    that uses titles for ranking — this is part of the routing fix."""
    q = next(t for t in tools_list if t.name == "query")
    title = q.annotations.title or ""
    assert "fallback" in title.lower(), (
        f"`query` tool title should signal fallback status; got: {title!r}"
    )


def test_plot_tools_are_not_read_only(tools_list):
    """Volcano and Venn tools write PNGs to disk in local mode. They should
    NOT advertise readOnlyHint=True."""
    for name in ("create_volcano_plot", "create_venn_diagram"):
        t = next(tt for tt in tools_list if tt.name == name)
        assert t.annotations.readOnlyHint is False, (
            f"{name} writes files; readOnlyHint should be False, "
            f"got {t.annotations.readOnlyHint}"
        )


def test_data_tools_are_read_only(tools_list):
    """The data-fetch tools only issue READ_ACCESS Neo4j queries. They must
    advertise readOnlyHint=True so clients don't gate them behind a write
    confirmation prompt."""
    read_only_expected = {
        "get_neo4j_schema",
        "get_node_metadata",
        "get_relationship_metadata",
        "get_study_info",
        "select_assays",
        "find_differentially_expressed_genes",
        "find_differentially_methylated_regions",
        "find_differentially_abundant_organisms",
        "find_common_differentially_expressed_genes",
        "find_common_differentially_methylated_regions",
        "find_common_differentially_abundant_organisms",
        "find_common_de_genes_overlapping_dm_regions",
        "query",
        "fetch_plot",
    }
    for name in read_only_expected:
        t = next(tt for tt in tools_list if tt.name == name)
        assert t.annotations.readOnlyHint is True, (
            f"{name} only reads from Neo4j; readOnlyHint should be True, "
            f"got {t.annotations.readOnlyHint}"
        )

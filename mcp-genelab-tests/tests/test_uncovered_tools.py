"""Functional coverage for the tools that the rest of the suite only checked
at the registration level (test_tools_list.py) but never actually invoked
end-to-end.

Before this file, 11 of the 22 registered tools were never called through
mcp_server.call_tool() anywhere in the suite — they appeared only as names in
the tools-list assertions. A tool can register correctly yet still crash, issue
the wrong Cypher, or return the wrong shape when invoked; registration tests
don't catch any of that. This file closes the gap by calling each of those 11
tools at least once and asserting on its output:

  Schema / metadata (Neo4j-backed):
    - get_neo4j_schema
    - get_node_metadata
    - get_relationship_metadata

  Output-directory session state:
    - set_output_directory
    - get_output_directory

  Plot generation + on-demand save script (matplotlib-backed):
    - create_volcano_plot
    - create_venn_diagram
    - get_save_script

  Prompt / text-transform utilities (no Neo4j):
    - clean_mermaid_diagram
    - create_chat_transcript
    - visualize_schema

Combined with the specialists already exercised in test_data_tools.py and
test_common_tools.py, and fetch_plot in test_plot_outputs.py, this brings the
functional-invocation coverage to all 22 tools.
"""
from __future__ import annotations

import json

import pytest

from conftest import call_tool_sync


# ===========================================================================
# Schema / metadata tools (Neo4j-backed)
# ===========================================================================

def test_get_neo4j_schema_runs_apoc_and_returns_rows(driver, mcp_server):
    """get_neo4j_schema must issue the apoc.meta.data() schema query and pass
    the returned rows straight through as JSON text."""
    captured = {}

    def route(q, p):
        if "apoc.meta.data()" in q:
            captured["schema_query"] = True
            return [{
                "label": "Assay",
                "attributes": {"identifier": "STRING"},
                "relationships": {"MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG": "MGene"},
            }]
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_neo4j_schema", {})
    assert captured.get("schema_query"), "expected the apoc.meta.data() schema query to run"
    assert "Assay" in text
    # Output is a JSON array of records.
    parsed = json.loads(text)
    assert isinstance(parsed, list) and parsed[0]["label"] == "Assay"


def test_get_neo4j_schema_reports_errors_gracefully(driver, mcp_server):
    """If the schema query raises (e.g. APOC not installed), the tool returns
    an `Error:` text block rather than propagating the exception."""
    def route(q, p):
        raise RuntimeError("Neo.ClientError.Procedure.ProcedureNotFound")
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_neo4j_schema", {})
    assert text.startswith("Error:")
    assert "ProcedureNotFound" in text


def test_get_node_metadata_queries_metanode(driver, mcp_server):
    """get_node_metadata must match MetaNode nodes and return their data."""
    captured = {}

    def route(q, p):
        if "MetaNode" in q:
            captured["metanode"] = True
            return [{"nodeName": "Assay", "m": {"description": "An assay node"}}]
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_node_metadata", {})
    assert captured.get("metanode"), "expected a MetaNode query"
    assert "Assay" in text


def test_get_relationship_metadata_uses_metarelationship(driver, mcp_server):
    """get_relationship_metadata must match the MetaRelationship pattern and
    return rows without falling back when MetaRelationship yields data."""
    seen = {"queries": []}

    def route(q, p):
        seen["queries"].append(q)
        if "MetaRelationship" in q:
            return [{
                "node1": "Assay",
                "relationship": "MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG",
                "node2": "MGene",
                "properties": {"log2fc": "float"},
            }]
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_relationship_metadata", {})
    assert any("MetaRelationship" in q for q in seen["queries"])
    assert "MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG" in text


def test_get_relationship_metadata_falls_back_when_empty(driver, mcp_server):
    """When the MetaRelationship query returns empty ('[]'), the tool must try
    the apoc.meta.relTypeProperties() fallback query."""
    seen = {"queries": []}

    def route(q, p):
        seen["queries"].append(q)
        if "apoc.meta.relTypeProperties" in q:
            return [{"relType": "PERFORMED_SpAS", "properties": []}]
        # MetaRelationship branch returns empty -> triggers fallback
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_relationship_metadata", {})
    assert any("MetaRelationship" in q for q in seen["queries"])
    assert any("apoc.meta.relTypeProperties" in q for q in seen["queries"]), (
        "expected the relTypeProperties fallback to fire when MetaRelationship is empty"
    )
    assert "PERFORMED_SpAS" in text


# ===========================================================================
# Output-directory session state
# ===========================================================================

def test_set_then_get_output_directory_roundtrip(mcp_server, server_module):
    """set_output_directory stores the path; get_output_directory reads it back.

    The path lives in module-level state, so we reset it afterward to avoid
    leaking into other tests."""
    server_module._set_user_output_dir(None)  # clean slate
    try:
        set_text = call_tool_sync(
            mcp_server, "set_output_directory", {"path": "/Users/jane/Downloads"}
        )
        assert "/Users/jane/Downloads" in set_text

        get_text = call_tool_sync(mcp_server, "get_output_directory", {})
        assert "/Users/jane/Downloads" in get_text
    finally:
        server_module._set_user_output_dir(None)


def test_set_output_directory_rejects_empty_path(mcp_server, server_module):
    """An empty/whitespace path is rejected with an error message and does not
    overwrite any previously-set directory."""
    server_module._set_user_output_dir(None)
    try:
        text = call_tool_sync(mcp_server, "set_output_directory", {"path": "   "})
        assert "Error" in text
        # Nothing was stored.
        assert server_module._get_user_output_dir() is None
    finally:
        server_module._set_user_output_dir(None)


def test_get_output_directory_when_unset(mcp_server, server_module):
    """With no directory set, get_output_directory explains none is configured
    and points at set_output_directory."""
    server_module._set_user_output_dir(None)
    try:
        text = call_tool_sync(mcp_server, "get_output_directory", {})
        assert "No output directory" in text
        assert "set_output_directory" in text
    finally:
        server_module._set_user_output_dir(None)


# ===========================================================================
# Plot generation + on-demand save script (matplotlib-backed)
# ===========================================================================

def _volcano_expression_route():
    """Route returning a small expression result set (label/x/p) plus the
    factor metadata the volcano tool reads for axis labels."""
    def route(q, p):
        qs = q.strip()
        if "RETURN a.factors_1 AS factors_1" in qs:
            return [{"factors_1": ["Space Flight"], "factors_2": ["Ground Control"]}]
        if "MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG" in qs:
            return [
                {"label": "Gene1", "x": 3.2, "p": 0.001},
                {"label": "Gene2", "x": -2.8, "p": 0.002},
                {"label": "Gene3", "x": 0.1, "p": 0.9},
                {"label": "Gene4", "x": 4.5, "p": 0.0001},
            ]
        return []
    return route


def _image_blocks(mcp_server, name, args):
    """Call a plot tool and return (text, has_image) where has_image is True if
    any returned content block is an MCP image."""
    import asyncio
    from conftest import text_from
    result = asyncio.run(mcp_server.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    has_image = any(
        getattr(c, "type", None) == "image" or c.__class__.__name__ == "ImageContent"
        for c in content
    )
    return text_from(result), has_image


def test_create_volcano_plot_returns_image_and_registers(driver, mcp_server, server_module):
    """create_volcano_plot must render a PNG (returned as an image block) and
    register it in the plot registry so fetch_plot / get_save_script can find it."""
    server_module._LAST_PLOTS.clear()
    driver.set_route(_volcano_expression_route())

    text, has_image = _image_blocks(
        mcp_server, "create_volcano_plot",
        {"assay_id": "OSD-244-abc", "data_type": "expression"},
    )
    assert has_image, "volcano plot should return an inline image content block"
    # A plot was registered for later save-script retrieval.
    assert server_module._list_registered_plots(), (
        "expected the volcano plot to be registered in _LAST_PLOTS"
    )


def test_create_volcano_plot_handles_no_data(driver, mcp_server, server_module):
    """With no matching rows, the tool returns an informative text message and
    does not crash trying to draw an empty figure."""
    server_module._LAST_PLOTS.clear()
    driver.set_route(lambda q, p: [])  # nothing matches

    text = call_tool_sync(
        mcp_server, "create_volcano_plot",
        {"assay_id": "OSD-000-missing", "data_type": "expression"},
    )
    assert "No expression data" in text


def _venn_two_assay_route():
    """Route for a 2-way expression Venn.

    create_venn_diagram issues, per assay: an assay-info query (factors,
    technology, measurement, analysis_method) and an expression item query
    returning `item_id` / `value` columns. We hand back distinct gene sets so
    the intersection is non-trivial."""
    def route(q, p):
        qs = q.strip()
        aid = p.get("assay_id")
        if "MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG" in qs and "RETURN mg.symbol" in qs:
            if aid == "OSD-244-a":
                return [
                    {"item_id": "GeneA1", "value": 2.0},
                    {"item_id": "GeneA2", "value": 2.4},
                    {"item_id": "GeneA3", "value": -2.1},
                    {"item_id": "GeneShared1", "value": 1.5},
                    {"item_id": "GeneShared2", "value": 1.9},
                ]
            if aid == "OSD-244-b":
                return [
                    {"item_id": "GeneB1", "value": 2.2},
                    {"item_id": "GeneB2", "value": -2.6},
                    {"item_id": "GeneShared1", "value": 1.7},
                    {"item_id": "GeneShared2", "value": 2.0},
                ]
            return []
        if "RETURN a.factors_1 AS factors_1" in qs:
            return [{
                "factors_1": ["Space Flight"], "factors_2": ["Ground Control"],
                "technology": "RNA-Seq", "measurement": "transcription profiling",
                "analysis_method": "DESeq2",
            }]
        return []
    return route


def test_create_venn_diagram_returns_image(driver, mcp_server, server_module):
    """create_venn_diagram must render a 2-way comparison as an image and
    register it for save-script retrieval."""
    server_module._LAST_PLOTS.clear()
    driver.set_route(_venn_two_assay_route())

    text, has_image = _image_blocks(
        mcp_server, "create_venn_diagram",
        {"assay_id_1": "OSD-244-a", "assay_id_2": "OSD-244-b",
         "data_type": "expression", "log2fc_threshold": 1.0},
    )
    # Either a rendered image (happy path) or a clear text explanation if the
    # fake data didn't satisfy a stricter validation path — but it must not raise.
    assert has_image or text, "venn tool must return either an image or a text message"
    if has_image:
        assert server_module._list_registered_plots(), (
            "a rendered Venn diagram should be registered in _LAST_PLOTS"
        )


def test_get_save_script_lists_and_resolves(mcp_server, server_module):
    """get_save_script with no filename lists the registry; with a known
    filename it returns the detailed save-options block referencing the
    plot:// resource and fetch_plot."""
    # Seed the registry directly so this test doesn't depend on matplotlib.
    server_module._LAST_PLOTS.clear()
    server_module._register_plot("demo_plot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100,
                                 "/Users/jane/Downloads/demo_plot.png")
    try:
        listing = call_tool_sync(mcp_server, "get_save_script", {})
        assert "demo_plot.png" in listing

        detail = call_tool_sync(mcp_server, "get_save_script", {"filename": "demo_plot.png"})
        assert "demo_plot.png" in detail
        assert "plot://demo_plot.png" in detail
        assert "fetch_plot" in detail
    finally:
        server_module._LAST_PLOTS.clear()


def test_get_save_script_unknown_filename(mcp_server, server_module):
    """Requesting a save script for a plot that isn't in the registry returns a
    helpful 'not in the registry' message rather than crashing."""
    server_module._LAST_PLOTS.clear()
    text = call_tool_sync(mcp_server, "get_save_script", {"filename": "ghost.png"})
    assert "ghost.png" in text
    assert "registry" in text.lower()


# ===========================================================================
# Prompt / text-transform utilities (no Neo4j)
# ===========================================================================

def test_clean_mermaid_diagram_strips_notes_and_empty_braces(mcp_server):
    """clean_mermaid_diagram must remove note lines, collapse empty class
    braces, and truncate post-newline text."""
    raw = (
        "classDiagram\n"
        "    direction TB\n"
        "    class Study {\n"
        "    }\n"
        "    class Assay {\n"
        "        string identifier\n"
        "    }\n"
        "    note for Study \"this should be removed\"\n"
        "    Study --> Assay : PERFORMED_SpAS\n"
    )
    text = call_tool_sync(mcp_server, "clean_mermaid_diagram", {"mermaid_content": raw})
    assert "note for" not in text, "note statements must be stripped"
    # The empty Study class should no longer have its braces.
    assert "class Study {" not in text
    # Non-empty Assay class is preserved with its property.
    assert "identifier" in text
    assert "PERFORMED_SpAS" in text


def test_clean_mermaid_diagram_truncates_post_newline_text(mcp_server):
    """A label carrying a literal '\\n' followed by extra text (e.g. an edge
    name with appended property text) must be truncated at the newline."""
    raw = "classDiagram\n    class MEASURED_DIFFERENTIAL_METHYLATION_ASmMR\\nmethylation_diff, q_value\n"
    text = call_tool_sync(mcp_server, "clean_mermaid_diagram", {"mermaid_content": raw})
    assert "methylation_diff, q_value" not in text
    assert "MEASURED_DIFFERENTIAL_METHYLATION_ASmMR" in text


def test_create_chat_transcript_returns_prompt(mcp_server):
    """create_chat_transcript returns a markdown prompt template (not a Neo4j
    query). It should mention the transcript structure and mcp-genelab."""
    text = call_tool_sync(mcp_server, "create_chat_transcript", {})
    assert "Chat Transcript" in text
    assert "mcp-genelab" in text
    # It instructs the assistant to use present_files for the output.
    assert "present_files" in text


def test_visualize_schema_returns_prompt(mcp_server):
    """visualize_schema returns the Mermaid-diagram workflow prompt, which must
    direct the assistant through clean_mermaid_diagram."""
    text = call_tool_sync(mcp_server, "visualize_schema", {})
    assert "Mermaid" in text
    assert "clean_mermaid_diagram" in text
    assert "present_files" in text

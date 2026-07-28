"""Functional tests for the single-assay specialist tools.

These tests invoke each tool end-to-end against the FakeDriver, verify that
the tool's output contains the right markers (table headers, gene names,
counts, CSV blocks), AND inspect the actual Cypher queries that were issued
to catch behavior changes that wouldn't show up in the rendered output (e.g.
LIMIT clause appearing when top_n=None)."""
from __future__ import annotations

from conftest import call_tool_sync


# --- get_study_info -------------------------------------------------------

def test_get_study_info_renders_metadata_and_assays(driver, mcp_server):
    """get_study_info must show study identifier, project title, organism,
    the data-type summary table, the per-assay table, and an inline CSV."""

    def route(q, p):
        qs = q.strip()
        if qs.startswith("MATCH (s:Study {identifier:"):
            if "PERFORMED_SpAS" in qs:
                return [
                    {"assay_id": "OSD-48-rna", "name": "A1",
                     "technology": "RNA-Seq",
                     "measurement": "transcription profiling",
                     "analysis_method": "DESeq2",
                     "factors_1": ["Space Flight", "Carcass"],
                     "factors_2": ["Ground Control", "Carcass"],
                     "material_1": "liver", "material_2": "liver"},
                    {"assay_id": "OSD-48-wgbs", "name": "A2",
                     "technology": "WGBS",
                     "measurement": "DNA methylation profiling",
                     "analysis_method": "methylKit",
                     "factors_1": ["Space Flight", "Carcass"],
                     "factors_2": ["Ground Control", "Carcass"],
                     "material_1": "liver", "material_2": "liver"},
                ]
            return [{
                "identifier": "OSD-48", "name": "OSD-48 test",
                "project_title": "TEST PROJECT",
                "project_type": "Spaceflight",
                "description": "A test description.",
                "organism": "Mus musculus", "taxonomy": "10090",
                "host_organism": None, "host_strain": None,
                "missions": ["RR-1"],
            }]
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "get_study_info", {"study_id": "OSD-48"})
    assert "OSD-48" in text
    assert "TEST PROJECT" in text
    assert "OSD-48-rna" in text
    assert "OSD-48-wgbs" in text
    assert "Data Types" in text
    assert "```csv" in text  # CSV side-channel


# --- find_differentially_expressed_genes ----------------------------------

def _deg_route_factory():
    """Build a route function for the DEG tool that returns:
       - 20 ranked upregulated rows (so we can verify the LIMIT cap)
       - 20 ranked downregulated rows
       - total counts of 347 up / 247 down (the actual OSD-48 numbers from
         the production-data audit)"""
    def route(q, p):
        qs = q.strip()
        if "RETURN a.factors_1 AS factors_1" in qs:
            return [{"factors_1": ["SF", "Carcass"],
                     "factors_2": ["GC", "Carcass"]}]
        if "log2fc > 0" in qs and "count(*)" in qs:
            return [{"total": 347}]
        if "log2fc < 0" in qs and "count(*)" in qs:
            return [{"total": 247}]
        if "log2fc > 0" in qs:
            limit = p.get("top_n", 999)
            return [{
                "gene_symbol": f"Up{i}", "gene_name": f"upgene{i}",
                "log2fc": 5.0 - i*0.01, "adj_p_value": 1e-10,
                "group_mean_1": 10.0, "group_stdev_1": 1.0,
                "group_mean_2": 1.0, "group_stdev_2": 0.5,
            } for i in range(min(limit, 20))]
        if "log2fc < 0" in qs:
            limit = p.get("top_n", 999)
            return [{
                "gene_symbol": f"Dn{i}", "gene_name": f"dngene{i}",
                "log2fc": -5.0 + i*0.01, "adj_p_value": 1e-10,
                "group_mean_1": 1.0, "group_stdev_1": 0.5,
                "group_mean_2": 10.0, "group_stdev_2": 1.0,
            } for i in range(min(limit, 20))]
        return []
    return route


def test_deg_top_n_default_uses_limit(driver, mcp_server):
    """With top_n=10, the Cypher must contain LIMIT $top_n and the output
    must show 'showing 10 of TOTAL' for each direction."""
    driver.set_route(_deg_route_factory())
    text = call_tool_sync(
        mcp_server, "find_differentially_expressed_genes",
        {"assay_id": "OSD-48-rna", "top_n": 10},
    )
    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert limit_calls, "Cypher should include LIMIT $top_n when top_n=10"
    assert "showing 10 of 347" in text
    assert "showing 10 of 247" in text
    assert "Up0" in text and "Dn0" in text
    assert "Up10" not in text, "should be capped at top_n=10"


def test_deg_top_n_none_drops_limit_and_returns_all(driver, mcp_server):
    """With top_n=None, the LIMIT clause must be dropped from the Cypher AND
    top_n must not appear in the params dict. The stub returns 20 rows;
    the tool must show all of them."""
    driver.set_route(_deg_route_factory())
    text = call_tool_sync(
        mcp_server, "find_differentially_expressed_genes",
        {"assay_id": "OSD-48-rna", "top_n": None},
    )
    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert not limit_calls, (
        "Cypher should NOT include LIMIT $top_n when top_n=None"
    )
    top_n_in_params = any("top_n" in (c[1] or {}) for c in driver.calls)
    assert not top_n_in_params, (
        "top_n should NOT appear in params when LIMIT was dropped"
    )
    for i in range(20):
        assert f"Up{i}" in text, f"Up{i} missing from unlimited output"


# --- find_differentially_methylated_regions -------------------------------

def test_dmr_with_promoter_filter(driver, mcp_server):
    """DMR with in_promoter=True must:
       - propagate `mr.in_promoter = $mr_in_promoter` into every per-direction
         query and the count queries
       - render both hypermethylated and hypomethylated tables
       - drop the LIMIT clause when top_n=None"""

    def route(q, p):
        qs = q.strip()
        if qs.startswith("MATCH (a:Assay {identifier: $first_assay_id})"):
            return [{"factors_1": ["SF", "Carcass"],
                     "factors_2": ["GC", "Carcass"]}]
        if "count(*)" in qs and "methylation_diff >" in qs:
            return [{"total": 570}]
        if "count(*)" in qs and "methylation_diff < -" in qs:
            return [{"total": 100}]
        if "methylation_diff >" in qs:
            return [{
                "assay_id": "OSD-48-wgbs",
                "gene_symbol": f"H{i}", "gene_name": f"hyper{i}",
                "region": f"chr1:1000{i}", "in_promoter": True,
                "methylation_diff": 50.0 - i, "q_value": 1e-15,
                "group_mean_1": 80.0, "group_stdev_1": 5.0,
                "group_mean_2": 30.0, "group_stdev_2": 5.0,
            } for i in range(5)]
        if "methylation_diff < -" in qs:
            return [{
                "assay_id": "OSD-48-wgbs",
                "gene_symbol": f"L{i}", "gene_name": f"hypo{i}",
                "region": f"chr2:2000{i}", "in_promoter": True,
                "methylation_diff": -40.0 + i, "q_value": 1e-15,
                "group_mean_1": 20.0, "group_stdev_1": 5.0,
                "group_mean_2": 70.0, "group_stdev_2": 5.0,
            } for i in range(3)]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_differentially_methylated_regions",
        {"assay_id": "OSD-48-wgbs", "in_promoter": True, "top_n": None},
    )

    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert not limit_calls, "LIMIT $top_n should be dropped when top_n=None"

    promoter_calls = [
        c for c in driver.calls
        if "mr.in_promoter = $mr_in_promoter" in c[0]
    ]
    assert promoter_calls, "in_promoter filter must be applied in Cypher"

    assert "Hypermethylated Regions" in text
    assert "Hypomethylated Regions" in text
    assert "showing 5 of 570" in text
    assert "showing 3 of 100" in text
    assert "H0" in text and "L0" in text


# --- find_differentially_abundant_organisms -------------------------------

def test_abundance_top_n_none_returns_all(driver, mcp_server):
    """Abundance tool with top_n=None must drop LIMIT and render both
    increased and decreased tables."""

    def route(q, p):
        qs = q.strip()
        if qs.startswith("MATCH (a:Assay {identifier: $assay_id})\n"
                         "        RETURN a.factors_1"):
            return [{"factors_1": ["F1"], "factors_2": ["F2"]}]
        if "count(*)" in qs and "r.log2fc > $log2fc_threshold" in qs:
            return [{"total": 42}]
        if "count(*)" in qs and "r.log2fc < -$log2fc_threshold" in qs:
            return [{"total": 17}]
        if "r.log2fc > $log2fc_threshold" in qs:
            return [{
                "organism_name": f"OrgUp{i}", "organism_id": f"OID{i}",
                "log2fc": 3.0 - i*0.1, "lnfc": 2.0 - i*0.1,
                "q_value": 0.01, "adj_p_value": None,
                "group_mean_1": 100.0, "group_stdev_1": 10.0,
                "group_mean_2": 10.0, "group_stdev_2": 1.0,
            } for i in range(8)]
        if "r.log2fc < -$log2fc_threshold" in qs:
            return [{
                "organism_name": f"OrgDn{i}", "organism_id": f"OIDD{i}",
                "log2fc": -3.0 + i*0.1, "lnfc": None,
                "q_value": None, "adj_p_value": 0.01,
                "group_mean_1": 10.0, "group_stdev_1": 1.0,
                "group_mean_2": 100.0, "group_stdev_2": 10.0,
            } for i in range(4)]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_differentially_abundant_organisms",
        {"assay_id": "OSD-X", "top_n": None},
    )

    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert not limit_calls

    assert "Increased Abundance" in text
    assert "Decreased Abundance" in text
    assert "OrgUp0" in text and "OrgDn0" in text
    assert "showing 8 of 42" in text
    assert "showing 4 of 17" in text


# --- query() ---------------------------------------------------------------

def test_query_tool_executes_read_with_total_rows_header(driver, mcp_server):
    """The query tool must:
       - execute a MATCH-only query
       - prepend `total_rows: N` to the response
       - emit `rows:` and then the JSON array"""

    def route(q, p):
        if "PERFORMED_SpAS" in q:
            return [{"a.identifier": "OSD-48-aaa"},
                    {"a.identifier": "OSD-48-bbb"}]
        return []
    driver.set_route(route)

    text = call_tool_sync(mcp_server, "query", {
        "query": ("MATCH (s:Study)-[:PERFORMED_SpAS]->(a:Assay) "
                  "RETURN a.identifier")
    })
    assert "total_rows: 2" in text
    assert "rows:" in text
    assert "OSD-48-aaa" in text


def test_query_tool_blocks_writes(driver, mcp_server):
    """Write keywords must be rejected. This is enforced server-side rather
    than relying on Neo4j READ_ACCESS, so the rejection arrives before any
    transaction starts."""
    text = call_tool_sync(
        mcp_server, "query", {"query": "CREATE (n:Foo) RETURN n"}
    )
    assert "rejected" in text.lower()
    assert "read-only" in text.lower()


# --- select_assays --------------------------------------------------------

def _select_assays_route():
    def route(q, p):
        if "PERFORMED_SpAS" in q:
            return [
                {"assay_id": "OSD-48-aaa",
                 "f1": ["Space Flight", "Carcass"],
                 "f2": ["Ground Control", "Carcass"],
                 "technology": "RNA-Seq",
                 "measurement": "transcription profiling",
                 "method": "DESeq2",
                 "material_1": "liver", "material_2": "liver"},
                {"assay_id": "OSD-48-bbb",
                 "f1": ["Space Flight", "Carcass"],
                 "f2": ["Ground Control", "Carcass"],
                 "technology": "WGBS",
                 "measurement": "DNA methylation profiling",
                 "method": "methylKit",
                 "material_1": "liver", "material_2": "liver"},
            ]
        return []
    return route


def test_select_assays_list_mode_renders_factor_menu(driver, mcp_server):
    """List mode (no `selection` argument) returns a numbered menu of unique
    factor arrays."""
    driver.set_route(_select_assays_route())
    text = call_tool_sync(
        mcp_server, "select_assays", {"study_id": "OSD-48"}
    )
    assert "Factor arrays" in text
    assert "Ground Control,Carcass" in text
    assert "Space Flight,Carcass" in text


def test_select_assays_pick_mode_returns_matching_assays(driver, mcp_server):
    """Pick mode with `selection='2,1'` (Space Flight vs Ground Control, given
    alphabetic sort puts Ground Control at index 1 and Space Flight at index
    2) must return both matching assays with technology/measurement/method."""
    driver.set_route(_select_assays_route())
    text = call_tool_sync(
        mcp_server, "select_assays",
        {"study_id": "OSD-48", "selection": "2,1"},
    )
    assert "Selected Assays" in text
    assert "OSD-48-aaa" in text and "OSD-48-bbb" in text
    assert "RNA-Seq" in text and "WGBS" in text

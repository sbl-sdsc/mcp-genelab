"""Functional tests for the four cross-assay specialist tools.

These tools were the original gap that caused the LLM to fall back to `query`
for intersection / overlap questions. We test each one end-to-end and check
the intersection semantics directly: genes/organisms present in BOTH per-assay
result sets must appear in the output, genes/organisms present in only ONE
must NOT appear."""
from __future__ import annotations

from conftest import call_tool_sync


# --- find_common_differentially_expressed_genes ---------------------------

def test_common_deg_intersects_up_and_down_genes(driver, mcp_server):
    """The common-DEG tool must:
       - issue per-assay up and down queries (4 total for 2 assays)
       - compute intersections separately for up vs. down direction
       - keep only genes present in ALL listed assays
       - filter out genes present in only one assay"""

    def route(q, p):
        aid = p.get("assay_id")
        qs = q.strip()
        if "m.log2fc > $log2fc_threshold" in qs:
            if aid == "OSD-X-a":
                return [{"gene_symbol": "Foxp3", "log2fc": 3.0},
                        {"gene_symbol": "Cux2",  "log2fc": 2.5},
                        {"gene_symbol": "OnlyA", "log2fc": 4.0}]
            if aid == "OSD-X-b":
                return [{"gene_symbol": "Foxp3", "log2fc": 2.9},
                        {"gene_symbol": "Cux2",  "log2fc": 1.8},
                        {"gene_symbol": "OnlyB", "log2fc": 3.5}]
        if "m.log2fc < -$log2fc_threshold" in qs:
            if aid == "OSD-X-a":
                return [{"gene_symbol": "Apobec2", "log2fc": -10.0},
                        {"gene_symbol": "Dbp",     "log2fc": -4.5}]
            if aid == "OSD-X-b":
                return [{"gene_symbol": "Apobec2", "log2fc": -8.0}]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_common_differentially_expressed_genes",
        {"assay_ids": ["OSD-X-a", "OSD-X-b"],
         "log2fc_threshold": 1.0, "adj_p_threshold": 0.05},
    )

    # Intersection contents
    assert "Foxp3" in text and "Cux2" in text   # both up in both assays
    assert "Apobec2" in text                    # down in both
    assert "Dbp" not in text                    # down in A only
    assert "OnlyA" not in text                  # up in A only
    assert "OnlyB" not in text                  # up in B only

    # Counts
    assert "Total common upregulated genes:** 2" in text
    assert "Total common downregulated genes:** 1" in text

    # CSV side-channel
    assert "```csv" in text


# --- find_common_differentially_methylated_regions ------------------------

def test_common_dmr_intersects_hyper_and_hypo_genes(driver, mcp_server):
    """The common-DMR tool must intersect hyper and hypo separately and
    propagate the in_promoter filter into the Cypher."""

    def route(q, p):
        aid = p.get("assay_id")
        qs = q.strip()
        if "r.methylation_diff > $meth_threshold" in qs:
            if aid == "OSD-X-m1":
                return [{"gene_symbol": "SharedHyper",
                         "methylation_diff": 25.0},
                        {"gene_symbol": "OnlyHyper1",
                         "methylation_diff": 30.0}]
            if aid == "OSD-X-m2":
                return [{"gene_symbol": "SharedHyper",
                         "methylation_diff": 22.0},
                        {"gene_symbol": "OnlyHyper2",
                         "methylation_diff": 28.0}]
        if "r.methylation_diff < -$meth_threshold" in qs:
            if aid == "OSD-X-m1":
                return [{"gene_symbol": "SharedHypo",
                         "methylation_diff": -18.0}]
            if aid == "OSD-X-m2":
                return [{"gene_symbol": "SharedHypo",
                         "methylation_diff": -15.0}]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_common_differentially_methylated_regions",
        {"assay_ids": ["OSD-X-m1", "OSD-X-m2"],
         "in_promoter": True, "q_value_threshold": 0.05},
    )

    assert "SharedHyper" in text
    assert "SharedHypo" in text
    assert "OnlyHyper1" not in text
    assert "OnlyHyper2" not in text

    assert "Total common hypermethylated genes:** 1" in text
    assert "Total common hypomethylated genes:** 1" in text

    promoter_calls = [
        c for c in driver.calls
        if "mr.in_promoter = $mr_in_promoter" in c[0]
    ]
    assert promoter_calls, (
        "in_promoter=True must propagate into the Cypher filter block"
    )


# --- find_common_differentially_abundant_organisms ------------------------

def test_common_da_intersects_increased_and_decreased(driver, mcp_server):
    """The common-DA tool must intersect increased (log2fc>0) and decreased
    (log2fc<0) organisms separately."""

    def route(q, p):
        aid = p.get("assay_id")
        qs = q.strip()
        if "r.log2fc > $log2fc_threshold" in qs:
            if aid == "OSD-X-d1":
                return [{"organism_name": "OrgSharedUp", "log2fc": 2.5},
                        {"organism_name": "OrgOnly1",    "log2fc": 3.0}]
            if aid == "OSD-X-d2":
                return [{"organism_name": "OrgSharedUp", "log2fc": 2.8},
                        {"organism_name": "OrgOnly2",    "log2fc": 1.5}]
        if "r.log2fc < -$log2fc_threshold" in qs:
            if aid == "OSD-X-d1":
                return [{"organism_name": "OrgSharedDown",
                         "log2fc": -2.5}]
            if aid == "OSD-X-d2":
                return [{"organism_name": "OrgSharedDown",
                         "log2fc": -3.0}]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_common_differentially_abundant_organisms",
        {"assay_ids": ["OSD-X-d1", "OSD-X-d2"], "log2fc_threshold": 0.5},
    )

    assert "OrgSharedUp" in text and "OrgSharedDown" in text
    assert "OrgOnly1" not in text and "OrgOnly2" not in text
    assert "Total common increased:** 1" in text
    assert "Total common decreased:** 1" in text


# --- find_common_de_genes_overlapping_dm_regions --------------------------

def test_overlap_pooled_methylation_with_promoter_filter(driver, mcp_server):
    """The DE/DM overlap tool must:
       - issue DE queries on the expression_assay_id (single assay)
       - issue DM queries using `a.identifier IN $assay_ids` (pooled across
         the methylation assays list)
       - propagate in_promoter=True into the DM Cypher
       - emit all four directional quadrants (up_hyper, up_hypo, down_hyper,
         down_hypo)
       - embed a combined CSV with a `category` column distinguishing the
         four quadrants"""

    def route(q, p):
        qs = q.strip()
        # DE side: keyed by adj_p_value plus log2fc direction
        if ("r.log2fc > $log2fc_threshold" in qs
                and "r.adj_p_value" in qs):
            return [{"gene_symbol": "UpHyper",  "log2fc": 2.0},
                    {"gene_symbol": "UpHypo",   "log2fc": 2.5}]
        if ("r.log2fc < -$log2fc_threshold" in qs
                and "r.adj_p_value" in qs):
            return [{"gene_symbol": "DownHyper", "log2fc": -3.0},
                    {"gene_symbol": "DownHypo",  "log2fc": -2.0}]
        # DM side: keyed by methylation_diff direction
        if "r.methylation_diff > $meth_threshold" in qs:
            return [{"gene_symbol": "UpHyper",   "methylation_diff": 15.0},
                    {"gene_symbol": "DownHyper", "methylation_diff": 30.0}]
        if "r.methylation_diff < -$meth_threshold" in qs:
            return [{"gene_symbol": "UpHypo",    "methylation_diff": -12.0},
                    {"gene_symbol": "DownHypo",  "methylation_diff": -20.0}]
        return []
    driver.set_route(route)

    text = call_tool_sync(
        mcp_server, "find_common_de_genes_overlapping_dm_regions",
        {"expression_assay_id": "OSD-X-rna",
         "methylation_assay_id": ["OSD-X-meth1", "OSD-X-meth2"],
         "in_promoter": True,
         "log2fc_threshold": 1.0,
         "adj_p_threshold": 0.05,
         "methylation_diff_threshold": 0.0,
         "q_value_threshold": 0.05},
    )

    # Pooled DM query pattern
    pooled = [c for c in driver.calls if "a.identifier IN $assay_ids" in c[0]]
    assert pooled, (
        "DM side must use `a.identifier IN $assay_ids` for pooled "
        "methylation evidence"
    )

    # Promoter filter propagation
    promoter = [
        c for c in driver.calls
        if "mr.in_promoter = $mr_in_promoter" in c[0]
    ]
    assert promoter, "in_promoter filter must propagate to DM Cypher"

    # Pooled-summary text
    assert "pooled across 2" in text

    # All four directional quadrants
    assert "Upregulated & Hypermethylated"   in text
    assert "Upregulated & Hypomethylated"    in text
    assert "Downregulated & Hypermethylated" in text
    assert "Downregulated & Hypomethylated"  in text

    # Genes in correct quadrants
    assert "UpHyper"   in text
    assert "DownHyper" in text

    # CSV with category column
    assert "```csv" in text
    assert "up_hyper" in text and "down_hyper" in text

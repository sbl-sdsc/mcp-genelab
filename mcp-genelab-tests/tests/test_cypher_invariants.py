"""Cypher invariants — defensive checks on the SQL/Cypher the tools issue.

These tests inspect what queries actually run via driver.calls, not just what
the tool prints. They catch regressions that would otherwise hide behind
correct-looking output. Examples:

  - A future edit accidentally drops `IS NOT NULL` from the lnfc clause,
    which silently drops every DESeq2 row but the tool still renders a
    plausible (just smaller) table.
  - The conditional LIMIT clause regresses to always-include LIMIT, which
    crashes Neo4j with `LIMIT NULL` when top_n=None but otherwise looks fine.
  - The MR filter plumbing is wired to the wrong parameter name; the filter
    silently never matches, but counts and outputs still look reasonable.

We name these "invariants" because they're properties that must hold across
every release; the tests serve as the executable form of those promises."""
from __future__ import annotations

import pytest

from conftest import call_tool_sync


# --- Write-query blocking -------------------------------------------------

@pytest.mark.parametrize("verb", [
    "CREATE", "MERGE", "SET", "DELETE", "REMOVE", "ADD", "DROP",
])
def test_query_tool_rejects_each_write_verb(verb, driver, mcp_server):
    """Every keyword in the write-blocker regex must reject. If someone adds
    a new write verb to Cypher's vocabulary and forgets to update the
    regex, this parameterized test fails for the new verb."""
    text = call_tool_sync(
        mcp_server, "query",
        {"query": f"{verb} (n:Foo) RETURN n"},
    )
    # The query must be rejected. We assert on the stable behavioral signal
    # ("rejected" + "read-only") rather than the exact keyword, because the
    # rejection message deliberately no longer echoes which write verb tripped
    # (that echo was a minor info-leak on a public endpoint).
    assert "rejected" in text.lower()
    assert "read-only" in text.lower()


@pytest.mark.parametrize("verb_case", [
    "create", "Merge", "DeLeTe",
])
def test_query_tool_write_block_is_case_insensitive(
    verb_case, driver, mcp_server
):
    """Write blocking must be case-insensitive — a future user could lower-case
    a CREATE and bypass a naive blocker."""
    text = call_tool_sync(
        mcp_server, "query",
        {"query": f"{verb_case} (n:Foo) RETURN n"},
    )
    assert "rejected" in text.lower()


# --- lnfc null-safety in abundance queries --------------------------------

def test_abundance_lnfc_clause_handles_null_safely(driver, mcp_server):
    """The abundance tool's lnfc filter must be wrapped in
    `(r.lnfc IS NULL OR ...)` so DESeq2 rows (where lnfc IS NULL) are not
    silently dropped.

    We pass lnfc_threshold=0.5 and inspect the actual Cypher to confirm the
    IS NULL guard is present. Without this guard, every DESeq2 row falls
    out of the result with no warning."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    call_tool_sync(
        mcp_server, "find_differentially_abundant_organisms",
        {"assay_id": "OSD-X", "lnfc_threshold": 0.5, "top_n": 5},
    )

    lnfc_calls = [c for c in driver.calls if "$lnfc_threshold" in c[0]]
    assert lnfc_calls, (
        "When lnfc_threshold is set, the Cypher must reference "
        "$lnfc_threshold"
    )
    for q, _ in lnfc_calls:
        assert "r.lnfc IS NULL" in q, (
            "lnfc clause must guard NULL with `r.lnfc IS NULL OR ...` "
            "so DESeq2 rows aren't silently dropped. Got Cypher:\n" + q
        )


def test_abundance_lnfc_omitted_when_threshold_unset(driver, mcp_server):
    """When lnfc_threshold is None, the Cypher must NOT reference
    $lnfc_threshold — referencing an undefined param would either crash
    Neo4j or pass a None that some methods treat as 0."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    call_tool_sync(
        mcp_server, "find_differentially_abundant_organisms",
        {"assay_id": "OSD-X", "top_n": 5},  # lnfc_threshold defaults to None
    )

    for q, p in driver.calls:
        assert "$lnfc_threshold" not in q, (
            "Cypher must NOT reference $lnfc_threshold when the parameter "
            "wasn't supplied. Got: " + q
        )


# --- Conditional LIMIT invariants -----------------------------------------

@pytest.mark.parametrize("tool_name,extra_args", [
    ("find_differentially_expressed_genes", {"assay_id": "OSD-X"}),
    ("find_differentially_methylated_regions", {"assay_id": "OSD-X"}),
    ("find_differentially_abundant_organisms", {"assay_id": "OSD-X"}),
])
def test_limit_clause_dropped_when_top_n_is_none(
    tool_name, extra_args, driver, mcp_server,
):
    """For each of the three nullable-top_n tools, top_n=None must drop the
    LIMIT clause AND omit top_n from the params dict. Both halves of the
    contract must hold — keeping LIMIT but not passing top_n would crash
    Neo4j on `LIMIT NULL`."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    args = dict(extra_args)
    args["top_n"] = None
    call_tool_sync(mcp_server, tool_name, args)

    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert not limit_calls, (
        f"{tool_name}: LIMIT $top_n must be dropped when top_n=None"
    )

    top_n_in_params = any("top_n" in (c[1] or {}) for c in driver.calls)
    assert not top_n_in_params, (
        f"{tool_name}: top_n must be omitted from params when top_n=None "
        f"(otherwise the dropped LIMIT and the present param are out of sync)"
    )


@pytest.mark.parametrize("tool_name,extra_args", [
    ("find_differentially_expressed_genes", {"assay_id": "OSD-X"}),
    ("find_differentially_methylated_regions", {"assay_id": "OSD-X"}),
    ("find_differentially_abundant_organisms", {"assay_id": "OSD-X"}),
])
def test_limit_clause_present_when_top_n_set(
    tool_name, extra_args, driver, mcp_server,
):
    """The mirror invariant: when top_n IS set, the LIMIT clause must be
    present and top_n must be in the params dict."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    args = dict(extra_args)
    args["top_n"] = 5
    call_tool_sync(mcp_server, tool_name, args)

    limit_calls = [c for c in driver.calls if "LIMIT $top_n" in c[0]]
    assert limit_calls, (
        f"{tool_name}: LIMIT $top_n must appear when top_n=5"
    )

    top_n_calls = [c for c in driver.calls if (c[1] or {}).get("top_n") == 5]
    assert top_n_calls, (
        f"{tool_name}: top_n=5 must be in the params dict"
    )


# --- MethylationRegion filter plumbing ------------------------------------

@pytest.mark.parametrize("filter_name,filter_value,expected_param", [
    ("in_promoter",         True,  "$mr_in_promoter"),
    ("in_exon",             True,  "$mr_in_exon"),
    ("in_intron",           False, "$mr_in_intron"),
    ("dist_to_feature_max", 5000,  "$mr_dist_max"),
])
def test_dmr_mr_filters_propagate_to_cypher(
    filter_name, filter_value, expected_param, driver, mcp_server,
):
    """Each MethylationRegion filter on the DMR tool must reach the Cypher.
    A wiring mistake (e.g. parameter name typo) would silently skip filtering
    while still rendering plausible-looking output."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    args = {"assay_id": "OSD-X", filter_name: filter_value}
    call_tool_sync(
        mcp_server, "find_differentially_methylated_regions", args,
    )

    parameterized = [c for c in driver.calls if expected_param in c[0]]
    assert parameterized, (
        f"DMR tool: {filter_name}={filter_value!r} should appear as "
        f"{expected_param} in the Cypher. Got Cypher queries:\n"
        + "\n".join(c[0] for c in driver.calls)
    )


# --- Pooled-methylation invariant -----------------------------------------

def test_dmr_pooled_assays_uses_in_clause(driver, mcp_server):
    """When the DMR tool is called with a list of assay IDs, the Cypher must
    use `a.identifier IN $assay_ids` rather than `a.identifier = $assay_id`.
    A regression to single-assay form would silently return only the first
    assay's results."""

    def route(q, p):
        if "RETURN a.factors_1" in q:
            return [{"factors_1": [], "factors_2": []}]
        if "count(*)" in q:
            return [{"total": 0}]
        return []
    driver.set_route(route)

    call_tool_sync(
        mcp_server, "find_differentially_methylated_regions",
        {"assay_id": ["OSD-X-a", "OSD-X-b"]},
    )

    pooled_calls = [
        c for c in driver.calls if "a.identifier IN $assay_ids" in c[0]
    ]
    assert pooled_calls, (
        "DMR tool with assay_id=list must use `a.identifier IN $assay_ids`"
    )
    # And the param dict must carry the assay_ids list, not a scalar
    for q, p in pooled_calls:
        assert isinstance(p.get("assay_ids"), list), (
            f"params['assay_ids'] must be a list when pooling. Got: {p}"
        )
        assert len(p["assay_ids"]) == 2

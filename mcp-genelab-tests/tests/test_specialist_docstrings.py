"""Specialist-tool docstring lead-sentence regression tests.

Tool descriptions are read top-to-bottom by the LLM during tool selection.
The first sentence carries the most weight. If a specialist tool's docstring
gets rewritten to lead with passive language like "Returns the top-N..." or
"Find common..." (its original form), the routing weakens because the LLM no
longer sees an explicit instruction to use this tool instead of `query`.

These tests pin the leading sentence to "USE THIS TOOL (not the `query`
tool)" — the imperative form that wins tool selection."""
from __future__ import annotations

import pytest


# Tools that should explicitly route LLM traffic away from the `query` tool.
# Grouped by category so a failure tells you immediately what bucket regressed.

SINGLE_ASSAY_SPECIALISTS = [
    "find_differentially_expressed_genes",
    "find_differentially_methylated_regions",
    "find_differentially_abundant_organisms",
]

METADATA_SPECIALISTS = [
    "get_study_info",
    "select_assays",
]

CROSS_ASSAY_SPECIALISTS = [
    "find_common_differentially_expressed_genes",
    "find_common_differentially_methylated_regions",
    "find_common_differentially_abundant_organisms",
    "find_common_de_genes_overlapping_dm_regions",
]

ALL_SPECIALISTS = (
    SINGLE_ASSAY_SPECIALISTS
    + METADATA_SPECIALISTS
    + CROSS_ASSAY_SPECIALISTS
)


@pytest.mark.parametrize("name", ALL_SPECIALISTS)
def test_specialist_has_routing_first_lead_sentence(tools_list, name):
    """Each specialist tool's docstring must lead with USE THIS TOOL within
    the first 300 characters — far enough back to allow some preamble but
    close enough to the front that the LLM weights it heavily."""
    t = next(x for x in tools_list if x.name == name)
    head = (t.description or "")[:300]
    assert "USE THIS TOOL" in head, (
        f"{name}: routing-first lead sentence missing.\n"
        f"first 300 chars: {head!r}"
    )


@pytest.mark.parametrize("name", ALL_SPECIALISTS)
def test_specialist_explicitly_redirects_from_query(tools_list, name):
    """The lead must specifically redirect the LLM away from the `query`
    tool — not just say USE THIS TOOL in a vacuum. We require both phrases
    to co-occur so the routing direction is unambiguous."""
    t = next(x for x in tools_list if x.name == name)
    head = (t.description or "")[:500]
    assert "USE THIS TOOL" in head and "`query` tool" in head, (
        f"{name}: lead sentence must redirect from `query` tool explicitly.\n"
        f"first 500 chars: {head!r}"
    )

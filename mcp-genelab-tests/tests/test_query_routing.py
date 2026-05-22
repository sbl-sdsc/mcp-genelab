"""Routing-policy regression tests.

The single highest-leverage routing signal is the `query` tool's description,
which the LLM reads via tools/list. If someone weakens the routing language
in a future edit (e.g. removes the FALLBACK keyword, drops a specialist tool
from the arrow list, or rewrites it as gentle advice), the routing breaks
silently — no exception, no error, just the LLM falling back to `query` for
the categories the specialists handle.

These tests are the brittle-on-purpose guard against that regression."""
from __future__ import annotations

import inspect


# --- query() tool description ---------------------------------------------

def test_query_description_starts_with_fallback(tools_list):
    """The first word of the description has disproportionate weight in tool
    selection. If FALLBACK isn't the leading word, the routing is weakened."""
    q = next(t for t in tools_list if t.name == "query")
    desc = (q.description or "").lstrip()
    assert desc.startswith("FALLBACK"), (
        f"`query` description must lead with FALLBACK; got: {desc[:80]!r}"
    )


def test_query_description_contains_do_not_clause(tools_list):
    """An explicit DO NOT clause raises the bar for the LLM to choose `query`
    for categories the specialists cover. Removing it returns the tool to a
    soft 'this is one option among many' framing."""
    q = next(t for t in tools_list if t.name == "query")
    assert "DO NOT" in (q.description or ""), (
        "`query` description must contain an explicit 'DO NOT' clause"
    )


def test_query_description_uses_arrow_notation(tools_list):
    """Arrow notation (`→`) is significantly more scannable than prose; we
    rely on it for the routing rules to stay legible."""
    q = next(t for t in tools_list if t.name == "query")
    assert "→" in (q.description or ""), (
        "`query` description must use arrow notation for routing rules"
    )


SPECIALIST_TOOLS_QUERY_MUST_MENTION = [
    "find_differentially_expressed_genes",
    "find_differentially_methylated_regions",
    "find_differentially_abundant_organisms",
    "find_common_differentially_expressed_genes",
    "find_common_differentially_methylated_regions",
    "find_common_differentially_abundant_organisms",
    "find_common_de_genes_overlapping_dm_regions",
    "get_study_info",
    "select_assays",
]


def test_query_description_mentions_each_specialist(tools_list):
    """Every specialist tool that should win over `query` must be NAMED in
    the `query` docstring. If we add a new specialist tool, this test fails
    until we update the routing rules — that's intentional, since a new
    specialist tool that isn't in the routing list is a routing leak."""
    q = next(t for t in tools_list if t.name == "query")
    desc = q.description or ""
    missing = [s for s in SPECIALIST_TOOLS_QUERY_MUST_MENTION if s not in desc]
    assert not missing, (
        f"`query` description must mention each specialist by name; "
        f"missing: {missing}"
    )


def test_query_description_points_at_select_assays_for_assay_id_lookup(
    tools_list,
):
    """A common failure mode is the LLM writing Cypher to discover assay IDs
    from a study ID, instead of calling select_assays/get_study_info first.
    The docstring must explicitly forbid that pattern."""
    q = next(t for t in tools_list if t.name == "query")
    desc = q.description or ""
    assert "study identifier" in desc.lower(), (
        "`query` docstring must call out the study-id-to-assay-id pattern "
        "and point at select_assays/get_study_info"
    )


def test_query_description_explains_what_specialists_give_back(tools_list):
    """The model is more likely to honor the routing rule if it knows what
    it loses by ignoring it. The docstring must articulate the specialists'
    value-add (formatted markdown + inline CSV)."""
    q = next(t for t in tools_list if t.name == "query")
    desc = q.description or ""
    assert "CSV" in desc, (
        "`query` docstring must mention the CSV side-channel the "
        "specialists provide"
    )
    assert "markdown" in desc.lower(), (
        "`query` docstring must mention the markdown table the specialists "
        "provide"
    )


# --- Server-level instructions ---------------------------------------------

def test_default_instructions_contains_tool_selection_policy(server_module):
    """The server-level `instructions` string is surfaced by clients as
    system-prompt-level guidance to the LLM. It must contain the explicit
    tool-selection policy."""
    src = inspect.getsource(server_module.async_main)
    assert "TOOL SELECTION POLICY" in src, (
        "DEFAULT_INSTRUCTIONS in async_main must contain the "
        "TOOL SELECTION POLICY block"
    )


def test_default_instructions_names_every_routing_target(server_module):
    """The instructions block must name every specialist tool by its
    function name. Naming the tools is what lets the LLM resolve 'use the
    specialist' to a specific tool call."""
    src = inspect.getsource(server_module.async_main)
    for name in [
        "find_differentially_expressed_genes",
        "find_differentially_methylated_regions",
        "find_differentially_abundant_organisms",
        "get_study_info",
        "select_assays",
    ]:
        assert name in src, (
            f"DEFAULT_INSTRUCTIONS must name {name} explicitly"
        )


def test_default_instructions_uses_imperative_language(server_module):
    """Soft language like 'consider using' weakens the routing. The
    instructions must use imperative language."""
    src = inspect.getsource(server_module.async_main)
    assert "ALWAYS call" in src, (
        "DEFAULT_INSTRUCTIONS should use imperative 'ALWAYS call' language"
    )
    assert "NEVER call" in src, (
        "DEFAULT_INSTRUCTIONS should use imperative 'NEVER call' language "
        "to forbid query-tool fallback for specialist categories"
    )

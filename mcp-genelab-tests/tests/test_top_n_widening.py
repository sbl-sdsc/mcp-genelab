"""top_n nullability regression tests.

The three single-assay specialist tools (DEG, DMR, abundance) must accept
top_n=None to mean "return all rows passing the filters" — without this, an
LLM serving an "all significant DEGs" request has no path through the
specialist and ends up falling back to the `query` tool.

The contract has three parts that all need to hold:
  (1) The JSON Schema for top_n must accept null.
  (2) The description must tell the LLM how to use that — without the
      description, the LLM may pass None successfully but not realize it
      can/should.
  (3) The default must remain 10 so existing callers that omit top_n keep
      their previous behavior.

If any of these regresses, the LLM's available paths through the specialist
narrow, and traffic leaks to `query`."""
from __future__ import annotations

import pytest


TOOLS_WITH_OPTIONAL_TOP_N = [
    "find_differentially_expressed_genes",
    "find_differentially_methylated_regions",
    "find_differentially_abundant_organisms",
]


def _top_n_schema(tools_list, name: str) -> dict:
    t = next(x for x in tools_list if x.name == name)
    return t.inputSchema["properties"]["top_n"]


def _accepts_null(schema: dict) -> bool:
    """Pydantic v2 emits Optional[int] as either:
       {"anyOf": [{"type": "integer"}, {"type": "null"}], ...}
    or, in some older settings:
       {"type": ["integer", "null"], ...}
    Accept both shapes."""
    if "anyOf" in schema:
        return any(s.get("type") == "null" for s in schema["anyOf"])
    t = schema.get("type")
    if isinstance(t, list):
        return "null" in t
    return t == "null"


@pytest.mark.parametrize("name", TOOLS_WITH_OPTIONAL_TOP_N)
def test_top_n_accepts_null(tools_list, name):
    """top_n must be expressible as null in JSON Schema so MCP clients
    pass-through None correctly."""
    schema = _top_n_schema(tools_list, name)
    assert _accepts_null(schema), (
        f"{name}: top_n schema must accept null. Got: {schema}"
    )


@pytest.mark.parametrize("name", TOOLS_WITH_OPTIONAL_TOP_N)
def test_top_n_default_is_ten(tools_list, name):
    """The default must remain 10 so existing callers' behavior is
    preserved. (A future change to a different default would be a
    backward-incompatible change worth flagging.)"""
    schema = _top_n_schema(tools_list, name)
    assert schema.get("default") == 10, (
        f"{name}: top_n default must be 10 for backward compatibility. "
        f"Got: {schema.get('default')}"
    )


@pytest.mark.parametrize("name", TOOLS_WITH_OPTIONAL_TOP_N)
def test_top_n_description_explains_none_semantics(tools_list, name):
    """A null-accepting schema alone is necessary but not sufficient — the
    LLM needs to know what None *means*. The description must explain the
    'pass None for all rows' contract explicitly."""
    schema = _top_n_schema(tools_list, name)
    desc = (schema.get("description") or "").lower()
    assert "none" in desc, (
        f"{name}: top_n description must mention None"
    )
    assert "all" in desc, (
        f"{name}: top_n description must explain that None returns ALL rows"
    )

# mcp-genelab test suite

This directory contains the pytest suite that runs on every commit to
`server.py`. Tests are fully offline — no real Neo4j, no network, no MCP
transport — so they run in roughly 10 seconds and are safe to run in CI on
every push.

## What's covered

Each file targets a distinct concern. When a test fails, the file name
should tell you immediately what regressed.

| File | What it guards |
|---|---|
| `test_tools_list.py` | All 22 tools register; every tool has title + readOnlyHint + idempotentHint + openWorldHint; titles are unique; the right tools are read-only vs. file-writing. |
| `test_query_routing.py` | The `query` tool's docstring leads with FALLBACK, uses arrow notation, names every specialist, includes a DO NOT clause, and explains the CSV side-channel. The server-level `DEFAULT_INSTRUCTIONS` block exists and uses imperative ALWAYS/NEVER language. |
| `test_specialist_docstrings.py` | All 9 specialist tools (3 single-assay, 2 metadata, 4 cross-assay) lead with `USE THIS TOOL (not the \`query\` tool)`. |
| `test_top_n_widening.py` | `top_n` on the three single-assay tools (DEG/DMR/abundance) has `Optional[int]` JSON Schema (accepts null), default 10, and the description explains the `None = all rows` contract. |
| `test_data_tools.py` | End-to-end functional tests of `get_study_info`, `find_differentially_expressed_genes`, `find_differentially_methylated_regions`, `find_differentially_abundant_organisms`, `query`, and `select_assays` against a stub driver. |
| `test_common_tools.py` | End-to-end functional tests of the four cross-assay specialist tools, including correct intersection semantics (genes in BOTH assays kept; genes in only one filtered out). |
| `test_cypher_invariants.py` | Defensive checks on the Cypher actually issued: write-blocking is case-insensitive and covers every write verb; lnfc clause is null-safe; LIMIT clause is conditionally present; MR filter parameters reach the Cypher; pooled methylation uses `IN $assay_ids`. |
| `test_plot_outputs.py` | Plot delivery via MCP resources: the `plot://{filename}` resource template registers and round-trips, `fetch_plot` returns the registered PNG bytes (and errors helpfully for unknown names), save instructions stay compact and reference the resource URI, and volcano/Venn render at the same conservative dpi. |
| `test_uncovered_tools.py` | End-to-end functional invocation of the 11 tools the other files only checked for registration: `get_neo4j_schema`, `get_node_metadata`, `get_relationship_metadata`, `set_output_directory`, `get_output_directory`, `create_volcano_plot`, `create_venn_diagram`, `get_save_script`, `clean_mermaid_diagram`, `create_chat_transcript`, and `visualize_schema`. |

> **Tool coverage:** together with the specialists in `test_data_tools.py` / `test_common_tools.py` and `fetch_plot` in `test_plot_outputs.py`, every one of the 22 registered tools is now invoked through `mcp_server.call_tool(...)` by at least one test — registration alone is no longer the only thing checked for any tool.

## Why split this way

Failure isolation. If someone weakens the routing language in the `query`
docstring, exactly one file fails (`test_query_routing.py`) and the test
name names the specific routing rule that broke. If someone introduces a
subtle Cypher-shape bug — e.g. drops `IS NOT NULL` from the lnfc clause —
`test_cypher_invariants.py` fails with a Cypher-level error message, not a
"the table looks wrong somehow" assertion in a functional test.

## How to run

```bash
# From the repo root:
pip install -r requirements-test.txt        # one-time: test + runtime deps
pip install -e .                             # one-time: editable install of the server (optional)

pytest                                       # run everything (~10-15 s)
pytest tests/test_uncovered_tools.py         # one file
pytest tests/test_query_routing.py           # one file
pytest tests/test_specialist_docstrings.py::test_specialist_has_routing_first_lead_sentence  # one parameter set
pytest -k "top_n"                            # everything mentioning top_n
pytest -k "volcano or venn"                  # everything about the plot tools
pytest -x -ra                                # stop on first failure, summarize
pytest --durations=10                        # show the 10 slowest tests
```

The conftest auto-detects the package at either `mcp_genelab/server.py` or `src/mcp_genelab/server.py`, so you don't need to set `PYTHONPATH`.

## How to extend

The conftest.py at the top of `tests/` provides three fixtures every test
should use:

- `driver` — a fresh `FakeDriver` per test. Set its `route` function to a
  callable mapping `(query_str, params_dict) -> list[dict]`. Every Cypher
  invocation is recorded in `driver.calls`.
- `mcp_server` — a FastMCP server wired to the per-test `driver`.
- `tools_list` — the result of `mcp_server.list_tools()`, materialized
  synchronously. Use for any test that inspects tool metadata without
  invoking a tool.

The helper `call_tool_sync(mcp_server, name, args)` invokes a tool and
returns the flattened text output as a single string for substring
assertions.

### Adding a new tool to the server

When you add a new `@mcp.tool()` decorator, you'll need to update at least:

1. `EXPECTED_TOOLS` in `test_tools_list.py` (add the new tool's name).
2. The right `readOnlyHint` bucket in `test_tools_list.py::test_data_tools_are_read_only`.
3. If the new tool is a specialist for a category currently handled by
   `query`, add it to `SPECIALIST_TOOLS_QUERY_MUST_MENTION` in
   `test_query_routing.py` and to one of the lists in
   `test_specialist_docstrings.py`. (Forgetting this is the regression
   we most want to catch — a new specialist that isn't in the routing
   rules creates a leak to `query`.)
4. If it has the `Optional[int] top_n` pattern, add it to
   `TOOLS_WITH_OPTIONAL_TOP_N` in `test_top_n_widening.py`.
5. Add at least one **functional** test that actually calls the tool via
   `call_tool_sync(mcp_server, "<tool_name>", {...})` (or `mcp_server.call_tool`
   for tools that return images/resources). Registration tests in
   `test_tools_list.py` confirm the tool exists but never run its body — a tool
   can register cleanly and still crash or issue the wrong Cypher when invoked.
   Put general-purpose functional tests for non-specialist tools in
   `test_uncovered_tools.py` (or the topical file that best fits), and keep the
   "every tool is invoked somewhere" property intact.

### Adding a new Cypher invariant

For invariants you want to lock in across releases (e.g. "every query
must have an `IS NOT NULL` guard on `r.adj_p_value`"), add a test to
`test_cypher_invariants.py`. The pattern is:

1. Stub `driver` with a route that returns minimal valid rows.
2. Call the tool through `call_tool_sync`.
3. Inspect `driver.calls` for the property you want to enforce.

This catches bugs that survive functional tests because the output still
looks plausible.

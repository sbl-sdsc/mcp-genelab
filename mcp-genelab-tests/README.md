# mcp-genelab test suite

Drop this directory's contents into your mcp-genelab repo root. Supports both flat layout (`mcp_genelab/server.py`) and `src/` layout (`src/mcp_genelab/server.py`); the conftest detects automatically.

```
<repo-root>/
├── src/mcp_genelab/                 # OR mcp_genelab/ at repo root
│   ├── __init__.py
│   └── server.py
├── tests/                           # from this package
│   ├── conftest.py
│   ├── test_*.py
│   └── README.md
├── pytest.ini                       # from this package
├── requirements-test.txt            # from this package
└── .github/
    └── workflows/
        └── test.yml                 # from this package
```

## Run locally

The suite is fully offline — no Neo4j instance, no network, no MCP transport. It builds the FastMCP server in-process against a fake Neo4j driver, so you only need the package and its dependencies installed.

### 1. Install dependencies

From the repo root:

```bash
# Install the test-only dependencies (also pulls in the runtime deps the
# server imports: mcp, neo4j, pydantic, matplotlib, matplotlib-venn,
# adjustText, numpy).
pip install -r requirements-test.txt
```

If you are developing the server itself, install it in editable mode first so `import mcp_genelab` resolves to your working tree:

```bash
pip install -e .
```

> The conftest auto-detects the package whether it lives at `mcp_genelab/server.py` (flat layout) or `src/mcp_genelab/server.py` (src layout), so no `PYTHONPATH` tweaking is required.

### 2. Run the tests

```bash
# Run the whole suite (verbose, short tracebacks — configured in pytest.ini)
pytest
```

Useful variations:

```bash
# Run a single file
pytest tests/test_uncovered_tools.py

# Run a single test by node id
pytest tests/test_data_tools.py::test_get_study_info_renders_metadata_and_assays

# Run every test whose name matches a keyword
pytest -k "volcano or venn"

# Stop at the first failure, drop into more detail
pytest -x -ra

# Show the slowest 10 tests
pytest --durations=10
```

The full suite runs in roughly 10–15 seconds. A non-zero exit code means at least one test failed; the short traceback printed for each failure names the file, line, and assertion.

## What's covered (112 tests across 9 files)

| File | Tests | Concern |
|---|---|---|
| `test_tools_list.py` | 11 | Tool registration (all 22 tools incl. `fetch_plot`), annotation completeness, title uniqueness, read-only hints |
| `test_query_routing.py` | 9 | `query` docstring + `DEFAULT_INSTRUCTIONS` routing policy |
| `test_specialist_docstrings.py` | 18 | "USE THIS TOOL" lead sentence on all 9 specialists |
| `test_top_n_widening.py` | 9 | `Optional[int]` schema on DEG/DMR/abundance tools |
| `test_data_tools.py` | 9 | Functional tests of single-assay tools + `query` + `select_assays` |
| `test_common_tools.py` | 4 | Functional tests of all 4 cross-assay specialist tools |
| `test_cypher_invariants.py` | 23 | Cypher-level invariants (write-blocking incl. DROP, LIMIT, lnfc null-safety, MR filter plumbing, pooled `IN` clause) |
| `test_plot_outputs.py` | 12 | Plot delivery via MCP resources: `plot://` URI, `fetch_plot` tool, compact save instructions, dpi parity |
| `test_uncovered_tools.py` | 17 | Functional invocation of the 11 tools the other files only registered: schema/metadata (`get_neo4j_schema`, `get_node_metadata`, `get_relationship_metadata`), output-dir state (`set_output_directory`, `get_output_directory`), plot generation + save script (`create_volcano_plot`, `create_venn_diagram`, `get_save_script`), and the prompt/utility tools (`clean_mermaid_diagram`, `create_chat_transcript`, `visualize_schema`) |

### Tool coverage

All **22** registered tools are checked for registration and invoked end-to-end (`mcp_server.call_tool(...)`) by at least one test:

| Tool | Exercised in |
|---|---|
| `get_neo4j_schema` | `test_uncovered_tools.py` |
| `get_node_metadata` | `test_uncovered_tools.py` |
| `get_relationship_metadata` | `test_uncovered_tools.py` |
| `get_study_info` | `test_data_tools.py` |
| `select_assays` | `test_data_tools.py` |
| `query` | `test_data_tools.py`, `test_cypher_invariants.py` |
| `find_differentially_expressed_genes` | `test_data_tools.py` |
| `find_differentially_methylated_regions` | `test_data_tools.py`, `test_cypher_invariants.py` |
| `find_differentially_abundant_organisms` | `test_data_tools.py`, `test_cypher_invariants.py` |
| `find_common_differentially_expressed_genes` | `test_common_tools.py` |
| `find_common_differentially_methylated_regions` | `test_common_tools.py` |
| `find_common_differentially_abundant_organisms` | `test_common_tools.py` |
| `find_common_de_genes_overlapping_dm_regions` | `test_common_tools.py` |
| `create_volcano_plot` | `test_uncovered_tools.py` |
| `create_venn_diagram` | `test_uncovered_tools.py` |
| `fetch_plot` | `test_plot_outputs.py` |
| `get_save_script` | `test_uncovered_tools.py` |
| `set_output_directory` | `test_uncovered_tools.py` |
| `get_output_directory` | `test_uncovered_tools.py` |
| `clean_mermaid_diagram` | `test_uncovered_tools.py` |
| `create_chat_transcript` | `test_uncovered_tools.py` |
| `visualize_schema` | `test_uncovered_tools.py` |

The `plot://{filename}` resource template is additionally covered by `test_plot_outputs.py`.

> **Note:** these fixtures encode the live server's tool schemas (the `EXPECTED_TOOLS` set, the `Optional[int]` `top_n` types, and tool parameter names like `path`/`filename`). If the server's schemas change, re-check those assertions — a passing offline suite plus schema drift would otherwise go unnoticed.

## CI

The included `.github/workflows/test.yml` runs the suite on Python 3.10, 3.11, 3.12, and 3.13 on every push and PR.

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

```bash
pip install -r requirements-test.txt
pytest
```

## What's covered (95 tests across 8 files)

| File | Tests | Concern |
|---|---|---|
| `test_tools_list.py` | 11 | Tool registration (22 tools incl. `fetch_plot`), annotation completeness, title uniqueness |
| `test_query_routing.py` | 9 | `query` docstring + `DEFAULT_INSTRUCTIONS` routing policy |
| `test_specialist_docstrings.py` | 18 | "USE THIS TOOL" lead sentence on all 9 specialists |
| `test_top_n_widening.py` | 9 | `Optional[int]` schema on DEG/DMR/abundance tools |
| `test_data_tools.py` | 9 | Functional tests of single-assay tools + `query` + `select_assays` |
| `test_common_tools.py` | 4 | Functional tests of all 4 cross-assay specialist tools |
| `test_cypher_invariants.py` | 23 | Cypher-level invariants (write-blocking incl. DROP, LIMIT, lnfc null-safety, MR filter plumbing, pooled `IN` clause) |
| `test_plot_outputs.py` | 10 | Plot delivery via MCP resources: `plot://` URI, `fetch_plot` tool, compact save instructions, dpi parity |

## CI

The included `.github/workflows/test.yml` runs the suite on Python 3.10, 3.11, 3.12, and 3.13 on every push and PR.

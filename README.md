# MCP GeneLab Server

[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)
[![PyPI version](https://img.shields.io/pypi/v/mcp-genelab?label=PyPI)](https://pypi.org/project/mcp-genelab/)

A Model Context Protocol (MCP) server that converts natural language queries into [Cypher](https://neo4j.com/product/cypher-graph-query-language) queries and executes them against the configured Neo4j endpoints. Customized tools provide seamless access to the NASA [GeneLab Knowledge Graph](https://github.com/BaranziniLab/spoke_genelab) (spoke-genelab v0.3.1), enabling AI-assisted analysis of spaceflight experiments and their biological effects. This server allows researchers to query differential gene expression, DNA methylation, and differential organism abundance data from NASA's space biology experiments through natural language interactions with AI assistants like Claude.

The GeneLab Knowledge Graph integrates omics data from NASA's [Open Science Data Repository (OSDR)](https://science.nasa.gov/biological-physical/data/osdr/), with nodes that can be used as connectors to other knowledge graphs, such as the [SPOKE](https://spoke.ucsf.edu/) (Scalable Precision Medicine Open Knowledge Engine) knowledge graph. This integration connects spaceflight experimental results with a comprehensive biological context, including genes, proteins, anatomical structures, pathways, and diseases.

This server is part of the NSF-funded [Proto-OKN Project](https://www.proto-okn.net/) (Prototype Open Knowledge Network). It's an extension of the [Neo4j Cypher MCP server](https://github.com/neo4j-contrib/mcp-neo4j/tree/main/servers/mcp-neo4j-cypher).

## Table of Contents

- [Building and Querying the SPOKE-GeneLab Knowledge Graph](#building-and-querying-the-spoke-genelab-knowledge-graph)
  - [Video](#video)
  - [Presentation](#presentation)
- [Knowledge Graph Schema (v0.3.1)](#knowledge-graph-schema-v031)
- [Features](#features)
  - [Querying & Analysis](#querying--analysis)
  - [Visualization](#visualization)
  - [Infrastructure](#infrastructure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [Option A: Connect to the Remote mcp-genelab Server (coming soon)](#option-a-connect-to-the-remote-mcp-genelab-server-coming-soon)
  - [Option B: Run mcp-genelab Locally with STDIO](#option-b-run-mcp-genelab-locally-with-stdio)
    - [Step 1 — Install Neo4j Desktop and import the spoke-genelab-v0.3.1 KG](#step-1--install-neo4j-desktop-and-import-the-spoke-genelab-v031-kg)
    - [Step 2 — Install `uv` and configure mcp-genelab](#step-2--install-uv-and-configure-mcp-genelab)
  - [Configure MCP Tools (Claude Desktop)](#configure-mcp-tools-claude-desktop)
- [Docker Deployment](#docker-deployment)
  - [Build the MCP Server Image](#build-the-mcp-server-image)
  - [Run with Streamable HTTP Transport](#run-with-streamable-http-transport)
  - [Environment Variables](#environment-variables)
- [Example Queries](#example-queries)
  - [Knowledge Graph Overview & Class Diagram](#knowledge-graph-overview--class-diagram)
  - [Node and Relationship Metadata Examples](#node-and-relationship-metadata-examples)
  - [Differential Expression Analysis with MCP tools](#differential-expression-analysis-with-mcp-tools)
  - [Differential Expression and Differential Methylation Analysis with MCP tools](#differential-expression-and-differential-methylation-analysis-with-mcp-tools)
  - [Differential Abundance Analysis with MCP tools](#differential-abundance-analysis-with-mcp-tools)
  - [Cross-Graph Differential Expression and Associated Disease Analysis with MCP tools](#cross-graph-differential-expression-and-associated-disease-analysis-with-mcp-tools)
- [MCP Tools Reference](#mcp-tools-reference)
  - [Schema & metadata](#schema--metadata)
  - [Study / assay browsing](#study--assay-browsing)
  - [Single-assay analyses](#single-assay-analyses)
  - [Cross-assay analyses](#cross-assay-analyses)
  - [Cypher fallback](#cypher-fallback)
  - [Plot generation, delivery, and saving](#plot-generation-delivery-and-saving)
  - [Output paths](#output-paths)
  - [Mermaid & transcript utilities](#mermaid--transcript-utilities)
- [Security](#security)
- [Development](#development)
- [Testing](#testing)
- [Building and Publishing (maintainers only)](#building-and-publishing-maintainers-only)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)
  - [Common Issues](#common-issues)
- [License](#license)
- [Citation](#citation)
  - [Related Publications](#related-publications)
- [Acknowledgments](#acknowledgments)
  - [Funding](#funding)
  - [Related Projects](#related-projects)

## Building and Querying the SPOKE-GeneLab Knowledge Graph

### [Presentation](https://docs.google.com/presentation/d/1MOtXGO1_FGMIfZO1Fwd4B5hv2GZGweEpJrOCBpjsmLw)

## Knowledge Graph Schema (v0.3.1)

The SPOKE-GeneLab KG v0.3.1 contains the following node and relationship types:

**Nodes:** Study, Mission, Assay, MGene, Gene, MethylationRegion, Organism, Anatomy, CellType

**Relationships:**
- `(Mission)-[:CONDUCTED_MIcS]->(Study)` — Mission conducted a study
- `(Study)-[:PERFORMED_SpAS]->(Assay)` — Study performed an assay
- `(Assay)-[:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(MGene)` — Differential gene expression
- `(Assay)-[:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->(MethylationRegion)` — Differential methylation
- `(Assay)-[:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->(Organism)` — Differential organism abundance
- `(Assay)-[:INVESTIGATED_ASiA]->(Anatomy)` — Assay investigated an anatomical structure
- `(Assay)-[:INVESTIGATED_ASiCT]->(CellType)` — Assay investigated a cell type
- `(MGene)-[:IS_ORTHOLOG_MGiG]->(Gene)` — Model organism gene is ortholog of human gene
- `(MGene)-[:METHYLATED_IN_MGmMR]->(MethylationRegion)` — Gene is methylated in a region

## Features

### Querying & Analysis
- **Natural Language Querying**: Ask questions in plain English — no need to write complex graph queries
- **NASA GeneLab Queries**: Ask questions about spaceflight experiments in the NASA GeneLab knowledge graph
- **Differential Gene Expression Analysis**: Find genes that are upregulated or downregulated in spaceflight conditions compared to ground controls
- **DNA Methylation Data**: Access epigenetic changes observed in spaceflight experiments, including promoter / exon / intron / distance-to-feature filtering
- **Differential Organism Abundance**: Query amplicon/metagenomics data showing changes in microbial community composition during spaceflight (DESeq2 and ANCOM-BC method-aware significance filtering)
- **Cross-Assay Intersection Analyses**: Find features differentially detected across multiple assays — common DEGs, common DMRs, common DA organisms — each direction (up/down, hyper/hypo, increased/decreased) kept separate so the LLM can reason about them independently
- **Expression-Methylation Coupling**: Identify differentially expressed genes whose promoter (or other gene region) is also differentially methylated in matched assays; supports pooled DM evidence across multiple methylation assays
- **Multi-Organism Support**: Query data across multiple model organisms including mice, rats, and other species used in space research
- **Anatomy & Cell Type Filtering**: Filter results by specific anatomical structures (UBERON ontology) or cell types (Cell Ontology) used in experiments
- **Assay Selection**: Browse and filter assays by study, organism, technology, or measurement type
- **Biomedical Context Integration**: Connect spaceflight gene expression changes to pathways, diseases, and other biological knowledge from SPOKE
- **Federated Queries**: Combine data from GeneLab with other Neo4j knowledge graphs for comprehensive biomedical analysis

### Visualization
- **Volcano Plots**: Generate volcano plots showing differentially expressed genes / methylated regions / abundant organisms with significance thresholds
- **Venn Diagrams**: Create Venn diagrams comparing differentially expressed genes (or DMRs, or DA organisms) across 2 or 3 assays, including an `expression_methylation` 2×2 grid variant for paired transcriptomic-epigenomic comparisons
- **Plot Resource Layer**: Every generated plot is exposed as an MCP resource under `plot://<filename>` and via a `fetch_plot` tool, so clients can retrieve PNG bytes through a separate request from the tool call that produced them. A failed fetch can be retried without re-running the analysis
- **Schema Visualization**: Generate visual representations of the knowledge graph schema
- **Mermaid Class Diagrams**: Create and clean Mermaid-format class diagrams of the KG schema

### Infrastructure
- **Read-Only Enforcement**: All Neo4j sessions use `READ_ACCESS` mode — write operations are rejected at the Bolt protocol level, protecting the knowledge graph from modification
- **Multiple Transport Modes**: Supports STDIO (local), SSE, and Streamable HTTP (remote deployment)
- **Remote Deployment**: Deploy as a web service behind a TLS reverse proxy, accessible via HTTPS URL from any MCP client
- **Docker Support**: Build and deploy as a Docker container for consistent, reproducible environments
- **Multiple Access Methods**: Use through Claude Desktop, VS Code with GitHub Copilot, or any MCP-compatible client
- **Pre-configured Setup**: Ready-to-use mcp-genelab configuration files for a local STDIO connection to the spoke-genelab-v0.3.1 KG (a remote public endpoint is coming soon)

## Prerequisites

Before using mcp-genelab, ensure you have:

- **Client Application**: One of the following:
  - Claude Desktop or claude.ai (Pro or Max subscription) — connect via Settings → Connectors → Add Custom Connector
  - VS Code with GitHub Copilot — connect via MCP server settings
  - Any MCP client that supports the STDIO or Streamable HTTP transport
- **Connection to the spoke-genelab-v0.3.1 knowledge graph** via one of the two paths below:
  - **Remote public endpoint** (*coming soon*): Connect to a hosted mcp-genelab server over HTTPS — no local install required. See [Option A](#option-a-connect-to-the-remote-mcp-genelab-server-coming-soon).
  - **Local install with STDIO**: Run mcp-genelab on your own machine against a local Neo4j instance holding the spoke-genelab-v0.3.1 KG. See [Option B](#option-b-run-mcp-genelab-locally-with-stdio).

For the local STDIO setup you also need:
- **Operating System**: macOS, Linux, or Windows
- **Python 3.10+** and the [uv](https://docs.astral.sh/uv/) package manager
- **Neo4j Desktop** with the spoke-genelab-v0.3.1 KG imported (installation links are provided in [Option B](#option-b-run-mcp-genelab-locally-with-stdio))

## Quick Start

mcp-genelab can be reached two ways: by connecting to the **remote public endpoint** (coming soon) or by **running mcp-genelab locally with STDIO**. Both serve the `spoke-genelab-v0.3.1` knowledge graph.

### Option A: Connect to the Remote mcp-genelab Server (coming soon)

> **Status: Coming soon.** A public HTTPS endpoint for mcp-genelab is being prepared. When it is live, the URL will be published here and in the [mcp-genelab repository](https://github.com/sbl-sdsc/mcp-genelab). Until then, use [Option B](#option-b-run-mcp-genelab-locally-with-stdio) to run mcp-genelab locally.

Once the public endpoint is available, you will be able to connect to mcp-genelab without installing anything locally:

1. Open Claude Desktop (or claude.ai)
2. Go to **Settings → Connectors** (or **Manage Connectors**)
3. Click **Add Custom Connector**
4. Enter:
   - **Name**: `mcp-genelab`
   - **MCP Server URL**: *the public mcp-genelab endpoint (coming soon)*
5. Click **Save**
6. In the chat prompt, click the **+** button and toggle the **mcp-genelab** connector **on**

Then ask a question like: *"What organisms are represented in the spoke-genelab-v0.3.1 knowledge graph?"*

### Option B: Run mcp-genelab Locally with STDIO

Running mcp-genelab locally has two parts: (1) stand up a local Neo4j instance holding the `spoke-genelab-v0.3.1` KG, and (2) point mcp-genelab at it via STDIO.

#### Step 1 — Install Neo4j Desktop and import the spoke-genelab-v0.3.1 KG

1. **Install Neo4j Desktop** and create a `spoke-genelab` instance (with the APOC plugin) by following the [Neo4j Desktop installation instructions](https://github.com/BaranziniLab/spoke_genelab/blob/main/docs/neo4j_installation.md).
2. **Import the spoke-genelab-v0.3.1 KG** into that instance by following the [database import instructions](https://github.com/BaranziniLab/spoke_genelab/blob/main/docs/import_db.md). Name the database exactly `spoke-genelab-v0.3.1` and start the instance once the import completes.

Note the Bolt URI (default `bolt://localhost:7687`), username, and password of your running instance — you will need them in Step 2.

#### Step 2 — Install `uv` and configure mcp-genelab

Install `uv` if you don't already have it:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then configure your MCP client to launch mcp-genelab over STDIO. For **Claude Desktop**, go to `Claude → Settings → Developer → Edit Config` and  the following mcp-genelab entry:

```json
{
  "mcpServers": {
    "spoke-genelab-local": {
      "command": "uvx",
      "args": ["mcp-genelab"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "neo4jdemo",
        "NEO4J_DATABASE": "spoke-genelab-v0.3.1",
        "INSTRUCTIONS": "Query the spoke-genelab-v0.3.1 KG to identify NASA spaceflight experiments containing omics datasets, specifically differential gene expression (transcriptomics), DNA methylation (epigenomics), and Amplicon (metagenomics) data."
      }
    }
  }
}
```

For **VS Code with GitHub Copilot**, add the same mcp-genelab entry to your `.vscode/mcp.json` (note the top-level key is `servers`, not `mcpServers`):

```json
{
  "servers": {
    "spoke-genelab-local": {
      "command": "uvx",
      "args": ["mcp-genelab"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "neo4jdemo",
        "NEO4J_DATABASE": "spoke-genelab-v0.3.1",
        "INSTRUCTIONS": "Query the spoke-genelab-v0.3.1 KG to identify NASA spaceflight experiments containing omics datasets, specifically differential gene expression (transcriptomics), DNA methylation (epigenomics), and Amplicon (metagenomics) data."
      }
    }
  }
}
```

> **Note**: Set `NEO4J_USERNAME`, `NEO4J_PASSWORD`, and `NEO4J_URI` to match the Neo4j instance you started in Step 1. The `uvx` command automatically downloads and runs the latest published mcp-genelab from PyPI. Keep `NEO4J_DATABASE` set to `spoke-genelab-v0.3.1` to match the database name you imported.

### Configure MCP Tools (Claude Desktop)

From the top menu bar:
```
1. Select: Claude->Settings->Connectors
2. Click: Configure for the MCP endpoints you want to use
3. Select Tool permissions: Always allow
```

In the prompt dialog box, click the `+` button:
```
1. Turn off Web search
2. Toggle MCP services on/off as needed
```

<img src="https://raw.githubusercontent.com/asaravia-butler/mcp-genelab/blob/DEV/docs/images/select_mcp_server.png"
     alt="Tool Selector"
     width="300">

Use @kg_name to refer to a specific mcp server (for example, @mcp-genelab).

To create a transcript of a chat (see examples below), use the following prompt: 
```Create a chat transcript```. 
The transcript can then be downloaded in .md or .pdf format.

## Docker Deployment

### Build the MCP Server Image

```bash
cd mcp-genelab
docker build -t mcp-genelab:latest .
```

### Run with Streamable HTTP Transport

```bash
docker run \
  --name mcp-server \
  --publish=127.0.0.1:8000:8000 \
  --env NEO4J_URI=bolt://host.docker.internal:7687 \
  --env NEO4J_USERNAME=neo4j \
  --env NEO4J_PASSWORD=yourpassword \
  --env NEO4J_DATABASE=spoke-genelab-v0.3.1 \
  --env MCP_TRANSPORT=streamable-http \
  --env MCP_HOST=0.0.0.0 \
  --env MCP_PORT=8000 \
  --detach \
  mcp-genelab:latest
```

The MCP server is then accessible at `http://localhost:8000/mcp/`.


### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt connection URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `neo4jdemo` | Neo4j password |
| `NEO4J_DATABASE` | `spoke-genelab-v0.3.1` | Neo4j database name for the spoke-genelab-v0.3.1 KG |
| `MCP_TRANSPORT` | `stdio` | Transport mode: `stdio`, `sse`, `streamable-http`, or `http` |
| `MCP_HOST` | `127.0.0.1` | HTTP listener host (use `0.0.0.0` for Docker) |
| `MCP_PORT` | `8000` | HTTP listener port |
| `INSTRUCTIONS` | *(see source)* | System instructions for the LLM |

## Example Queries

*Each link below points to a chat transcript that demonstrates how to use the mcp-genelab server to query and analyze GeneLab data hosted in the spoke-genelab-v0.3.1 Neo4j Knowledge Graph.*

### Knowledge Graph Overview & Class Diagram 

[Overview of spoke-genelab v0.3.1](docs/examples/MCP-GL_KG_overview.md) 

### Node and Relationship Metadata Examples

[List spoke-genelab-v0.3.1 assay node, properties, and relationships](docs/examples/MCP-GL_AssayNode_DEedge.md) 

### Differential Expression Analysis with MCP tools

[OSD-244 DE analysis](docs/examples/OSD-244_DE.md)

### Differential Expression and Differential Methylation Analysis with MCP tools                                                                                                                 

[OSD-48 DE and DM analysis](docs/examples/OSD-48_DE_DM_pubmed.md) 

### Differential Abundance Analysis with MCP tools

[OSD-267 DA analysis](docs/examples/OSD-267_DA_pubmed.md)

### Cross-Graph Differential Expression and Associated Disease Analysis with MCP tools

[OSD-161 DE and spoke-okn disease analysis](docs/examples/OSD-161_DE_proto-okn.md)

>*Note: To perform this example query, you will need to add the NSF OKN MCP server to your MCP client by following the instructions at [https://okn.us/mcp](https://okn.us/mcp).* 

---

## MCP Tools Reference

The server exposes **22 tools** plus a `plot://{filename}` resource template. Tools are listed below grouped by category. The specialist tools should be preferred over the generic `query` tool — server.py's `DEFAULT_INSTRUCTIONS` carries a `TOOL SELECTION POLICY` that routes natural-language requests to the right specialist.

### Schema & metadata

| Tool | Description |
|------|-------------|
| `get_neo4j_schema` | List all node types, their attributes, and relationships in the knowledge graph |
| `get_node_metadata` | Get descriptions of all node types from MetaNode entries |
| `get_relationship_metadata` | Get descriptions of all relationship types and their properties |
| `visualize_schema` | Generate a visual schema diagram of the knowledge graph |

### Study / assay browsing

| Tool | Description |
|------|-------------|
| `get_study_info` | Get detailed information about a specific study and its assays (metadata + assay inventory) |
| `select_assays` | Browse and filter assays by study, organism, technology, or measurement; resolve a factor pair to one or more assay IDs |

### Single-assay analyses

| Tool | Description |
|------|-------------|
| `find_differentially_expressed_genes` | Find up/downregulated genes for a given assay (DESeq2) |
| `find_differentially_methylated_regions` | Find hyper/hypomethylated regions for a given assay, with optional `in_promoter` / `in_exon` / `in_intron` / distance filters |
| `find_differentially_abundant_organisms` | Find organisms with differential abundance for a given assay (DESeq2 and/or ANCOM-BC) |

### Cross-assay analyses

| Tool | Description |
|------|-------------|
| `find_common_differentially_expressed_genes` | Intersect DEGs across multiple assays (up and down directions kept separate) |
| `find_common_differentially_methylated_regions` | Intersect DMRs across multiple assays, with optional MethylationRegion filters |
| `find_common_differentially_abundant_organisms` | Intersect differentially abundant organisms across multiple assays |
| `find_common_de_genes_overlapping_dm_regions` | Identify DE genes whose promoter (or other region) is also differentially methylated — supports method-aware significance filtering and pooled-DM evidence across multiple methylation assays |

### Cypher fallback

| Tool | Description |
|------|-------------|
| `query` | Execute a read-only Cypher query on the Neo4j database. Fallback for questions the specialists don't cover — the `DEFAULT_INSTRUCTIONS` policy directs the LLM to use specialist tools first |

### Plot generation, delivery, and saving

| Tool / resource | Description |
|------|-------------|
| `create_volcano_plot` | Generate a volcano plot of differential expression / methylation / abundance results; PNG returned inline and registered for resource fetch |
| `create_venn_diagram` | Create a Venn diagram comparing DEGs / DMRs / DA organisms across 2 or 3 assays (also supports the `expression_methylation` 2×2 grid variant); PNG returned inline and registered for resource fetch |
| `fetch_plot` | Re-fetch the canonical PNG bytes of a previously generated plot from the in-memory registry (last 8 plots, FIFO eviction). Returns the bytes as an `EmbeddedResource` so clients can render them inline. Safe to call repeatedly — no Cypher, no matplotlib, no re-render |
| `get_save_script` | Return a markdown block with multiple save options for a previously generated plot: right-click save, ask-LLM-client-to-save, or fetch via the `plot://` resource URI |
| `plot://{filename}` (resource) | MCP resource template — clients fetch PNG bytes via `resources/read` on `plot://<suggested_filename>`. Decoupled from the tool response that generated the plot, so a failed fetch can be retried without re-running the analysis |

### Output paths

| Tool | Description |
|------|-------------|
| `set_output_directory` | Set the user-facing directory where plots and CSV files should be saved |
| `get_output_directory` | Return the currently configured output directory |

### Mermaid & transcript utilities

| Tool | Description |
|------|-------------|
| `clean_mermaid_diagram` | Clean and validate a Mermaid class diagram of the KG schema |
| `create_chat_transcript` | Export the current chat as a formatted transcript |

## Security

All Neo4j sessions are opened with `default_access_mode=READ_ACCESS`, which is enforced at the Bolt protocol level by Neo4j. Any write operation is rejected with the error: *"Write operations are not allowed for READ transactions."* This works on both Community and Enterprise Edition.

As a second layer of defense, the `query` tool includes a regex-based write filter (`_is_write_query()`) that catches the Cypher write keywords `MERGE`, `CREATE`, `SET`, `DELETE`, `REMOVE`, `ADD`, and `DROP` (case-insensitive) before the query is sent to Neo4j. All queries also use `session.execute_read()` for transaction-level read enforcement. The two layers — server-side regex + Bolt-level READ_ACCESS — protect the knowledge graph from modification even if one layer is bypassed.

---

## Development

[Instructions for local development](docs/development.md)

## Testing

The project ships a pytest suite (95 tests across 8 files) that runs offline — no Neo4j connection, no network, no MCP transport. It guards against regressions in tool registration, annotation completeness, routing-policy language in tool docstrings, Cypher invariants (read-only enforcement, conditional `LIMIT`, lnfc null-safety, MethylationRegion filter propagation, pooled `IN $assay_ids` clause for cross-assay queries), and the plot resource layer (`plot://` URI registration, `fetch_plot` round-trips, save-instruction size guarantees).

```bash
pip install -r mcp-genelab-tests/requirements-test.txt
pytest
```

The suite runs in roughly 10 seconds and is safe to run on every commit. A GitHub Actions workflow at `.github/workflows/test.yml` runs the suite on Python 3.10 through 3.13 for every push and pull request. See `mcp-genelab-tests/tests/README.md` for the per-file breakdown and instructions on adding tests for new tools.

## Building and Publishing (maintainers only)

[Instructions for building, testing, and publishing the mcp-genelab package on PyPI](docs/build_publish.md)

## API Reference

[mcp-genelab server API](docs/api.md)

## Troubleshooting

### Common Issues

**MCP server not appearing in Claude Desktop:**
- Ensure you've completely quit and restarted Claude Desktop (not just closed the window)
- Check that your JSON configuration is valid (attach your config file to a chat and ask it to fix any errors)
- Verify that `uvx` is installed and accessible in your PATH

**Connection errors:**
- Verify the Neo4j endpoint URL is correct and accessible
- For a local Neo4j Desktop instance, ensure the `spoke-genelab` instance is started and the `spoke-genelab-v0.3.1` database has finished loading
- Check that `NEO4J_DATABASE` is set to `spoke-genelab-v0.3.1` and matches the database name you imported

**Write operation rejected:**
- This is expected behavior. All sessions use READ_ACCESS mode. Write operations (CREATE, MERGE, SET, DELETE) are blocked at the Bolt protocol level.

**Performance issues:**
- Complex Cypher queries may take time to execute
- Consider breaking down complex queries into smaller parts
- Check the endpoint's documentation for query best practices

## License

This project is licensed under the BSD 3-Clause License. See the [LICENSE](LICENSE) file for details.

## Citation

If you use MCP GeneLab in your research, please cite the following works:

```bibtex
@software{rose2026mcp-genelab,
  title={MCP GeneLab Server},
  author={Rose, P.W. and Saravia-Butler, A.M. and Nelson, C.A. and Shi, Y. and Baranzini, S.E.},
  year={2026},
  url={https://github.com/sbl-sdsc/mcp-genelab}
}

@software{rose2026spoke-genelab,
  title={NASA SPOKE-GeneLab Knowledge Graph},
  author={Rose, P.W. and Nelson, C.A. and Saravia-Butler, A.M. and Gebre, S.G. and Soman, K. and Grigorev, K.A. and Sanders, L.M. and Costes, S.V. and Baranzini, S.E.},
  year={2026},
  url={https://github.com/BaranziniLab/spoke_genelab}
}
```

### Related Publications

- Nelson, C.A., Rose, P.W., Soman, K., Sanders, L.M., Gebre, S.G., Costes, S.V., Baranzini, S.E. (2025). "Nasa Genelab-Knowledge Graph Fabric Enables Deep Biomedical Analysis of Multi-Omics Datasets." *NASA Technical Reports*, 20250000723. [Link](https://ntrs.nasa.gov/citations/20250000723)

- Sanders, L., Costes, S., Soman, K., Rose, P., Nelson, C., Sawyer, A., Gebre, S., Baranzini, S. (2024). "Biomedical Knowledge Graph Capability for Space Biology Knowledge Gain." *45th COSPAR Scientific Assembly*, July 13-21, 2024. [Link](https://ui.adsabs.harvard.edu/abs/2024cosp...45.2183S/abstract)

## Acknowledgments

### Funding

This work is supported in part by:
- **National Science Foundation** Award [#2333819](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2333819): "Proto-OKN Theme 1: Connecting Biomedical information on Earth and in Space via the SPOKE knowledge graph"

### Related Projects

- [Proto-OKN Project](https://www.proto-okn.net/) - Prototype Open Knowledge Network initiative
- [NSF Open Knowledge Network](https://okn.us/) - 40+ intercnnected knowledge graphs supported by NSF
- [NASA Open Science Data Repository (OSDR)](https://osdr.nasa.gov/bio/repo) - Repository of multi-modal space life science data
- [NASA GeneLab KG, spoke-genelab](https://github.com/BaranziniLab/spoke_genelab) - Git Repository for creating the spoke-geneLab KG v0.3.1
- [Model Context Protocol](https://modelcontextprotocol.io/) - AI assistant integration standard
- [Original Neo4j Cypher MCP server](https://github.com/neo4j-contrib/mcp-neo4j/tree/main/servers/mcp-neo4j-cypher) - Base implementation reference

---

*For questions, issues, or contributions, please visit our [GitHub repository](https://github.com/sbl-sdsc/mcp-genelab).*

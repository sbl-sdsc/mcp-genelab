# MCP GeneLab Server

[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)
[![PyPI version](https://badge.fury.io/py/mcp_genelab.svg)](https://badge.fury.io/py/mcp-genelab)

A Model Context Protocol (MCP) server that converts natural language queries into [Cypher](https://neo4j.com/product/cypher-graph-query-language) queries and executes them against the configured Neo4j endpoints. Customized tools provide seamless access to the NASA [GeneLab Knowledge Graph](https://github.com/BaranziniLab/spoke_genelab), enabling AI-assisted analysis of spaceflight experiments and their biological effects. This server allows researchers to query differential gene expression and DNA methylation data from NASA's space biology experiments through natural language interactions with AI assistants like Claude.

The GeneLab Knowledge Graph with data from NASA's [GeneLab Data Repository](https://genelab.nasa.gov/), part of the NASA [Open Science Data Repository (OSDR)](https://science.nasa.gov/biological-physical/data/osdr/), can be integrated with biomedical knowledge from the [SPOKE](https://spoke.ucsf.edu/) (Scalable Precision Medicine Open Knowledge Engine) knowledge graph. This integration connects spaceflight experimental results with a comprehensive biological context, including genes, proteins, anatomical structures, pathways, and diseases.

This server is part of the NSF-funded [Proto-OKN Project](https://www.proto-okn.net/) (Prototype Open Knowledge Network). It's an extension of the [Neo4j Cypher MCP server](https://github.com/neo4j-contrib/mcp-neo4j/tree/main/servers/mcp-neo4j-cypher).

## Features

- **Natural Language Querying**: Ask questions in plain English - no need to write complex graph queries
- **NASA GeneLab Queries**: Ask questions about spaceflight experiments in the NASA GeneLab knowledge graph
- **Differential Gene Expression Analysis**: Find genes that are upregulated or downregulated in spaceflight conditions compared to ground controls
- **DNA Methylation Data**: Access epigenetic changes observed in spaceflight experiments
- **Multi-Organism Support**: Query data across multiple model organisms including mice, rats, and other species used in space research
- **Tissue-Specific Analysis**: Filter results by specific organs, tissues, or cell types
- **Biomedical Context Integration**: Connect spaceflight gene expression changes to pathways, diseases, and other biological knowledge from SPOKE
- **Federated Queries**: Combine data from GeneLab with other Neo4j knowledge graphs for comprehensive biomedical analysis
- **Multiple Access Methods**: Use through Claude Desktop, VS Code with GitHub Copilot, or programmatically via the MCP protocol
- **Pre-configured Endpoints**: Ready-to-use access to both local and remote Neo4j databases containing the GeneLab Knowledge Graph

## Prerequisites

Before installing the MCP server, ensure you have:

- **Operating System**: macOS, Linux, or Windows
- **Client Application**: One of the following:
  - Claude Desktop with Pro or Max subscription
  - VS Code Insiders with GitHub Copilot subscription
- **Neo4j Knowledge Graphs**:
  - For a local installation of the GeneLab KG see [setup](https://github.com/BaranziniLab/spoke_genelab)
  - For remote access to GeneLab and SPOKE KGs [request](https://github.com/sbl-sdsc/mcp-genelab/issues) credentials

## Installation

### Prerequisites

The MCP GeneLab server requires installing the `uv` package manager on your system. If you don't have it installed, run:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Check if `uv` is in the PATH
```bash
which uv
```

If `uv` is not found, add the following line to your `~/.zshrc` file for zsh or `/.bash_profile` for bash.
```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then reload the shell.
```bash
source ~/.zshrc  # or source ~/.bash_profile
```

If you are using `macOS 12`, you also need to install `realpath`.

To check if you have `realpath` installed, run:
```bash
which realpath
```

Download the [Homebrew installer](https://github.com/Homebrew/brew/releases/download/4.6.17/Homebrew-4.6.17.pkg) and click on the downloaded package to install Homebrew.

Then run the following command to install coreutils and check if `realpath` is available.
```bash
brew install coreutils
which realpath
```


# Windows
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> **Note**: Once `uv` is installed, the `uvx` command in the configuration below will automatically download and run the latest version of the MCP server from PyPI when needed.

### Claude Desktop Setup

**Recommended for most users**

1. **Download and Install Claude Desktop**

   Visit [https://claude.ai/download](https://claude.ai/download) and install Claude Desktop for your operating system.

   > **Requirements**: Claude Pro or Max subscription is required for MCP server functionality.

2. **Configure MCP Server**

   **Option A: Download Pre-configured File (Recommended)**

   Download the pre-configured `claude_desktop_config.json` file with Neo4j endpoints from the repository and copy it to the appropriate location:

   **macOS**:
   ```bash
   # Download the config file
   curl -o /tmp/claude_desktop_config.json https://raw.githubusercontent.com/sbl-sdsc/mcp-genelab/main/config/claude_desktop_config.json
   
   # Copy to Claude Desktop configuration directory
   cp /tmp/claude_desktop_config.json "$HOME/Library/Application Support/Claude/"
   ```

   **Windows PowerShell**:
   ```powershell
   # Download the config file
   Invoke-WebRequest -Uri "https://raw.githubusercontent.com/sbl-sdsc/mcp-genelab/main/config/claude_desktop_config.json" -OutFile "$env:TEMP\claude_desktop_config.json"
   
   # Copy to Claude Desktop configuration directory
   Copy-Item "$env:TEMP\claude_desktop_config.json" "$env:APPDATA\Claude\"
   ```

   **Option B: Manual Configuration**

   Alternatively, you can manually edit the configuration file in Claude Desktop. Navigate to `Claude->Settings->Developer->Edit Config`
   to edit it.

   Below is an example of how to configure local and remote Neo4j endpoints. For remotely hosted Neo4j servers, update the url, username, and password.

   ```json
   {
     "mcpServers": {
      "genelab-local-cypher": {
         "command": "uvx",
         "args": ["mcp-genelab"],
         "env": {
           "NEO4J_URI": "bolt://localhost:7687",
           "NEO4J_USERNAME": "neo4j",
           "NEO4J_PASSWORD": "neo4jdemo",
           "NEO4J_DATABASE": "spoke-genelab-v0.0.4",
           "INSTRUCTIONS": "Query the GeneLab Knowledge Graph to identify NASA spaceflight experiments containing omics datasets, specifically differential gene expression (transcriptomics) and DNA methylation (epigenomics) data."
         }
       },
       "genelab-remote-cypher": {
         "command": "uvx",
         "args": ["mcp-genelab"],
         "env": {
           "NEO4J_URI": "bolt://remote_url:7687",
           "NEO4J_USERNAME": "username",
           "NEO4J_PASSWORD": "password",
           "NEO4J_DATABASE": "spoke-genelab-v0.0.4",
           "INSTRUCTIONS": "Query the GeneLab Knowledge Graph to identify NASA spaceflight experiments containing omics datasets, specifically differential gene expression (transcriptomics) and DNA methylation (epigenomics) data."
         }
      }
   }
   ```

   > **Important**: If you have existing MCP server configurations, do not use Option A as it will overwrite your existing configuration. Instead, use Option B and manually merge the Neo4j endpoints with your existing `mcpServers` configuration.

3. **Restart Claude Desktop**

   After saving the configuration file, quit Claude Desktop completely and restart it. The application needs to restart to load the new configuration and start the MCP servers.

4. **Verify Installation**

   1. Launch Claude Desktop
   2. Navigate to `Claude->Settings->Connectors`
   3. Verify that the configured Neo4j endpoints appear in the connector list
   4. You can configure each service to always ask for permission or to run it unsupervised (recommended)

### VS Code Setup

**For advanced users and developers**

1. **Install VS Code Insiders**

   Download and install VS Code Insiders from [https://code.visualstudio.com/insiders/](https://code.visualstudio.com/insiders/)

   > **Note**: VS Code Insiders is required as it includes the latest MCP (Model Context Protocol) features.

2. **Install GitHub Copilot Extension**

   - Open VS Code Insiders
   - Sign in with your GitHub account
   - Install the GitHub Copilot extension

   > **Requirements**: GitHub Copilot subscription is required for MCP integration.

3. **Configure MCP Server**

   **Option A: Download Pre-configured File (Recommended)**

   Download the pre-configured `mcp.json` file from the repository and copy it to the appropriate location.

   **macOS**:
   ```bash
   # Download the config file
   curl -o /tmp/mcp.json https://raw.githubusercontent.com/sbl-sdsc/mcp-genelab/main/config/mcp.json

   # Copy to VS Code Insiders configuration directory
   cp /tmp/mcp.json "$HOME/Library/Application Support/Code - Insiders/User/mcp.json"
   ```
 > **Note**: VS Code Insiders mcp.json file is identical to the claude_desktop_config.json file, except "mcpServer" is replaced by "server".

4. **Use the MCP Server**

   1. Open a new chat window in VS Code
   2. Select **Agent** mode
   3. Choose **Claude Sonnet 4.5 or later** model for optimal performance
   4. The MCP servers will automatically connect and provide knowledge graph access


## Quick Start

Once configured, you can immediately start querying knowledge graphs through natural language prompts in Claude Desktop or VS Code chat interface. The AI assistant will automatically convert your natural language queries into appropriate Cypher queries, execute them against the configured endpoints, and return structured, interpretable results.

### Example Queries

1. **Querying Specific MCP Servers**

To direct a query to a specific MCP server, use the @ operator followed by the server name.
For example:

```
@genelab-remote-cypher
```
See [response](docs/examples.md#Query-1).

2. **Federated Queries Across Multiple Servers**

You can also perform federated queries across multiple MCP servers.

```
@genelab-remote-cypher @spokeokn-remote-cypher
```

This will execute the query across both servers and combine the results as applicable.

See [response](docs/examples.md#Query-2).

3. **Node Metadata**
```
Describe the Assay node and its properties in @genelab-remote-cypher, and include an example for a ground control vs. space flight comparison.
```
See [response](docs/examples.md#Query-3).

4. **Relationship Metadata**
```
Describe the MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG relationship and its properties in @genelab-remote-cypher, and include an example for a ground control vs. space flight comparison.
```
See [response](docs/examples.md#Query-4).

5. **High-level overview of the GeneLab KG**
```
Give a breakdown of missions, studies, and the type of technologies used in the studies.
 ```
See [response](docs/examples.md#Query-5).

6. **Find upregulated genes in space flight:**
```
Find the top 5 upregulated genes in mouse liver tissue during Space Flight.
```
See [response](docs/examples.md#Query-6).

## Development

### Installing from Source

If you want to run a development version:

```bash
# Clone the repository
git clone https://github.com/sbl-sdsc/mcp-genelab.git
cd mcp-genelab

# Install dependencies
uv sync
```

### Building and Publishing (maintainers only)

```bash
# Increment version number (major|minor|patch)
uv version --bump minor

# Build the package
uv build

# Publish to TestPyPI first (recommended)
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-YOUR_TEST_PYPI_TOKEN_HERE

# Test the deployment
For testing, add the following parameters to the `args` option.
  "args": [
    "--index-url",
    "https://test.pypi.org/simple/",
    "--extra-index-url",
    "https://pypi.org/simple/",
    "mcp-genelab"
  ]

# Publish to PyPI 
uv publish --token pypi-YOUR_PYPI_TOKEN_HERE
```

---

## API Reference

### Available Tools

#### `get_neo4j_schema`

Lists all nodes, their attributes, and their relationships to other nodes in the Neo4j database.

**Parameters:**
- None

**Returns:**
- JSON array containing node labels, their attributes (with data types), and relationships to other nodes

**Note:** If this fails with a message that includes "Neo.ClientError.Procedure.ProcedureNotFound", the APOC plugin needs to be installed and enabled on the Neo4j database.

#### `read_neo4j_cypher`

Executes a read-only Cypher query on the Neo4j database.

**Parameters:**
- `query` (string, required): The Cypher query to execute
- `params` (object, optional): Parameters to pass to the Cypher query for parameterized queries

**Returns:**
- JSON object containing query results

**Example:**
```cypher
MATCH (s:Study)-[:PERFORMED_SpAS]->(a:Assay)
WHERE s.organism = $organism
RETURN s.name, a.name
LIMIT 10
```

#### `get_node_metadata`

Retrieves metadata descriptions for all node types from MetaNode nodes in the knowledge graph.

**Parameters:**
- None

**Returns:**
- JSON array containing detailed descriptions of each node type's properties, including data types and semantic meanings

#### `get_relationship_metadata`

Retrieves descriptions of properties for all relationship types in the knowledge graph.

**Parameters:**
- None

**Returns:**
- JSON array containing descriptions of each relationship type and their properties

#### `find_upregulated_genes`

Finds genes that are upregulated in spaceflight conditions compared to ground control for a specific organism and tissue/organ/cell type.

**Parameters:**
- `organism` (string, required): The organism to search for (e.g., 'Mus musculus', 'Rattus norvegicus')
- `material` (string, required): The organ, tissue, or cell type to search for (e.g., 'liver', 'hippocampus', 'skeletal muscle')
- `factor_space_1` (string, optional): First experimental grouping, default: 'Ground Control'
- `factor_space_2` (string, optional): Second experimental grouping, default: 'Space Flight'
- `top_n` (integer, optional): Number of top upregulated genes to return, default: 100

**Returns:**
- JSON array of upregulated genes with symbols, log2 fold changes, adjusted p-values, and assay information

**Note:** Only compares experiments with matching experimental factors to ensure valid comparisons.

#### `find_downregulated_genes`

Finds genes that are downregulated in spaceflight conditions compared to ground control for a specific organism and tissue/organ/cell type.

**Parameters:**
- `organism` (string, required): The organism to search for (e.g., 'Mus musculus', 'Rattus norvegicus')
- `material` (string, required): The organ, tissue, or cell type to search for (e.g., 'liver', 'hippocampus', 'skeletal muscle')
- `factor_space_1` (string, optional): First experimental grouping, default: 'Ground Control'
- `factor_space_2` (string, optional): Second experimental grouping, default: 'Space Flight'
- `top_n` (integer, optional): Number of top downregulated genes to return, default: 100

**Returns:**
- JSON array of downregulated genes with symbols, log2 fold changes, adjusted p-values, and assay information

**Note:** Only compares experiments with matching experimental factors to ensure valid comparisons.

### Command Line Interface

The MCP GeneLab server is configured through environment variables. When using `uvx`, the configuration is specified in the MCP configuration file (e.g., `claude_desktop_config.json`).

**Required Environment Variables:**
- `NEO4J_URI`: Neo4j database connection URI (e.g., `bolt://localhost:7687`)
- `NEO4J_USERNAME`: Neo4j database username
- `NEO4J_PASSWORD`: Neo4j database password
- `NEO4J_DATABASE`: Neo4j database name (e.g., `spoke-genelab-v0.0.4`)
- `INSTRUCTIONS`: Custom instructions for the MCP server describing its purpose

## Troubleshooting

### Common Issues

**MCP server not appearing in Claude Desktop:**
- Ensure you've completely quit and restarted Claude Desktop (not just closed the window)
- Check that your JSON configuration is valid (use a JSON validator)
- Verify that `uvx` is installed and accessible in your PATH

**Connection errors:**
- Check your internet connection
- Verify the SPARQL endpoint URL is correct and accessible
- Some endpoints may have rate limits or temporary downtime

**Performance issues:**
- Complex SPARQL queries may take time to execute
- Consider breaking down complex queries into smaller parts
- Check the endpoint's documentation for query best practices

## License

This project is licensed under the BSD 3-Clause License. See the [LICENSE](LICENSE) file for details.

## Citation

If you use MCP GeneLab in your research, please cite the following works:

```bibtex
@software{rose2025mcp-genelab,
  title={MCP GeneLab Server},
  author={Rose, P.W. and Saravia-Butler, A.M. and Nelson, C.A. and Shi, Y. and Baranzini, S.E.},
  year={2025},
  url={https://github.com/sbl-sdsc/mcp-proto-okn}
}

@software{rose2025spoke-genelab,
  title={NASA SPOKE-GeneLab Knowledge Graph},
  author={Rose, P.W. and Nelson, C.A. and Saravia-Butler, A.M. and Gebre, S.G. and Soman, K. and Grigorev, K.A. and Sanders, L.M. and Costes, S.V. and Baranzini, S.E.},
  year={2025},
  url={https://github.com/BaranziniLab/spoke_genelab}
}
```

### Related Publications

- Nelson, C.A., Rose, P.W., Soman, K., Sanders, L.M., Gebre, S.G., Costes, S.V., Baranzini, S.E. (2025). "Nasa Genelab-Knowledge Graph Fabric Enables Deep Biomedical Analysis of Multi-Omics Datasets." *NASA Technical Reports*, 20250000723. [Link](https://ntrs.nasa.gov/citations/20250000723)

- Sanders, L., Costes, S., Soman, K., Rose, P., Nelson, C., Sawyer, A., Gebre, S., Baranzini, S. (2024). "Biomedical Knowledge Graph Capability for Space Biology Knowledge Gain." *45th COSPAR Scientific Assembly*, July 13-21, 2024. [Link](https://ui.adsabs.harvard.edu/abs/2024cosp...45.2183S/abstract)

## Acknowledgments

### Funding

This work is supported by:
- **National Science Foundation** Award [#2333819](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2333819): "Proto-OKN Theme 1: Connecting Biomedical information on Earth and in Space via the SPOKE knowledge graph"

### Related Projects

- [Proto-OKN Project](https://www.proto-okn.net/) - Prototype Open Knowledge Network initiative
- [NASA Open Science Data Repository (OSDR)](https://science.nasa.gov/biological-physical/data/osdr/) - Repository of multi-modal space life science data
- [NASA GeneLab Data Repository](https://genelab.nasa.gov/) - GeneLab data repository used to create the GeneLab KG
- [NASA GeneLab KG](https://github.com/BaranziniLab/spoke_genelab) - Git Repository for creating the GeneLab KG
- [Model Context Protocol](https://modelcontextprotocol.io/) - AI assistant integration standard
- [Original Neo4j Cypher MCP server](https://github.com/neo4j-contrib/mcp-neo4j/tree/main/servers/mcp-neo4j-cypher) - Base implementation reference

---

*For questions, issues, or contributions, please visit our [GitHub repository](https://github.com/sbl-sdsc/mcp-genelab).*

## API Reference

The mcp-genelab server exposes **22 tools** plus a `plot://{filename}` resource template. The tools are grouped below by category, matching the groupings in the project README. Specialist tools should be preferred over the generic `query` tool — `server.py`'s `DEFAULT_INSTRUCTIONS` carries a `TOOL SELECTION POLICY` that routes natural-language requests to the right specialist.

All Neo4j sessions are opened in read-only mode (`READ_ACCESS`), so no tool can modify the knowledge graph.

### Table of Contents

- [Schema & metadata](#schema--metadata)
  - [`get_neo4j_schema`](#get_neo4j_schema)
  - [`get_node_metadata`](#get_node_metadata)
  - [`get_relationship_metadata`](#get_relationship_metadata)
  - [`visualize_schema`](#visualize_schema)
- [Study / assay browsing](#study--assay-browsing)
  - [`get_study_info`](#get_study_info)
  - [`select_assays`](#select_assays)
- [Single-assay analyses](#single-assay-analyses)
  - [`find_differentially_expressed_genes`](#find_differentially_expressed_genes)
  - [`find_differentially_methylated_regions`](#find_differentially_methylated_regions)
  - [`find_differentially_abundant_organisms`](#find_differentially_abundant_organisms)
- [Cross-assay analyses](#cross-assay-analyses)
  - [`find_common_differentially_expressed_genes`](#find_common_differentially_expressed_genes)
  - [`find_common_differentially_methylated_regions`](#find_common_differentially_methylated_regions)
  - [`find_common_differentially_abundant_organisms`](#find_common_differentially_abundant_organisms)
  - [`find_common_de_genes_overlapping_dm_regions`](#find_common_de_genes_overlapping_dm_regions)
- [Cypher fallback](#cypher-fallback)
  - [`query`](#query)
- [Plot generation, delivery, and saving](#plot-generation-delivery-and-saving)
  - [`create_volcano_plot`](#create_volcano_plot)
  - [`create_venn_diagram`](#create_venn_diagram)
  - [`fetch_plot`](#fetch_plot)
  - [`get_save_script`](#get_save_script)
  - [`plot://{filename}` (resource)](#plotfilename-resource)
- [Output paths](#output-paths)
  - [`set_output_directory`](#set_output_directory)
  - [`get_output_directory`](#get_output_directory)
- [Mermaid & transcript utilities](#mermaid--transcript-utilities)
  - [`clean_mermaid_diagram`](#clean_mermaid_diagram)
  - [`create_chat_transcript`](#create_chat_transcript)

---

## Schema & metadata

### `get_neo4j_schema`

Lists all node types, their attributes, and their relationships to other nodes in the Neo4j database.

**Parameters:**
- None

**Returns:**
- JSON array containing node labels, their attributes (with data types), and relationships to other nodes

**Note:** If this fails with a message that includes "Neo.ClientError.Procedure.ProcedureNotFound", the APOC plugin needs to be installed and enabled on the Neo4j database.

### `get_node_metadata`

Retrieves metadata descriptions for all node types from MetaNode nodes in the knowledge graph.

**Parameters:**
- None

**Returns:**
- JSON array containing detailed descriptions of each node type's properties, including data types and semantic meanings

### `get_relationship_metadata`

Retrieves descriptions of properties for all relationship types in the knowledge graph.

**Parameters:**
- None

**Returns:**
- JSON array containing descriptions of each relationship type and their properties. Uses fallback approaches (e.g. `apoc.meta.relTypeProperties()`) if MetaRelationship nodes are not available.

### `visualize_schema`

Provides a prompt for visualizing the knowledge graph schema using a Mermaid class diagram.

**Parameters:**
- None

**Returns:**
- Detailed instructions for creating a Mermaid class diagram visualization of the knowledge graph schema

**Workflow:**
1. Call `get_neo4j_schema()` to retrieve classes and predicates
2. Generate raw Mermaid class diagram showing nodes, properties, and relationships
3. Set diagram direction to TB (top-to-bottom)
4. Pass diagram through the `clean_mermaid_diagram` tool
5. Present cleaned diagram inline in a mermaid code block
6. Create a `.mermaid` file with only the cleaned diagram code (no markdown fences)
7. Save to the user's output directory
8. Use the `present_files` tool to share the `.mermaid` file for rendering

**Requirements:**
- The `.mermaid` file must contain ONLY the Mermaid diagram code
- No markdown code fences in the `.mermaid` file
- No explanatory text in the `.mermaid` file
- File should start with `classDiagram`

---

## Study / assay browsing

### `get_study_info`

Returns detailed information about a specific study and its assays (metadata plus an assay inventory). Preferred over the `query` tool for study-metadata questions.

**Parameters:**
- `study_id` (string, required): Study identifier (e.g., 'OSD-267')

**Returns:**
- Markdown-formatted study metadata (title, description, organism, mission, factors, etc.) together with an inventory of the assays performed by the study

### `select_assays`

Interactive tool for resolving a study's factor-pair conditions to assay identifiers, rendered in markdown format. Preferred over the `query` tool for turning a factor-pair condition into concrete assay IDs.

**Parameters:**
- `study_id` (string, optional): Study identifier (e.g., 'OSD-253')
- `selection` (string, optional): Comma-separated list of indices for selection (e.g., '1,2,3,4')

**Returns:**
- **First call** (selection=None):
  - Prompts for study_id if missing
  - Returns a numbered menu as a markdown table showing unique factor combinations across all assays
- **Second call** (with selection):
  - Pairs consecutive indices: (i,j), (k,l), ..., (m,n)
  - Returns assay_id(s) for each pair comparison
  - Must provide an even number of indices

**Usage Pattern:**
1. Call without parameters (or with only `study_id`) to see available factor combinations
2. Select pairs of conditions to compare
3. Use returned assay_ids with other tools

**Suggested Next Steps:**
- For a single pair: find differentially expressed genes, create a volcano plot, map genes to pathways
- For multiple pairs: find differentially expressed genes for each comparison, create volcano plots, identify consistent changes, map genes to pathways
- For < 4 pairs: create a Venn diagram to show overlap

---

## Single-assay analyses

### `find_differentially_expressed_genes`

Returns the top-N upregulated and downregulated genes for a given assay (DESeq2).

**Parameters:**
- `assay_id` (string, required): Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')
- `top_n` (integer or null, optional): How many genes to display for each of the up- and down-regulated lists, default: 10. Pass `null` (or omit) to return ALL genes passing the `adj_p_value` filter rather than a top-N — useful when the user asks for "all significantly upregulated genes" or wants the complete filtered set as CSV.
- `adj_p_threshold` (number, optional): Adjusted p-value threshold for significance, default: 0.05. Only genes with `adj_p_value <= this value` are returned.

**Returns:**
- Markdown-formatted table containing:
  - Top-N upregulated genes (log2fc > 0, sorted highest first)
  - Top-N downregulated genes (log2fc < 0, sorted lowest first)
  - Gene symbols, log2 fold changes, and adjusted p-values
- When `top_n=null` and the full filtered set is large, the inline table may be capped for readability while a CSV side-channel contains every row passing the filter.

### `find_differentially_methylated_regions`

Returns the top-N hyper- and hypo-methylated regions for a given assay (or a pool of assays), with optional location and distance filters.

**Parameters:**
- `assay_id` (string or array of strings, required): A single assay identifier (e.g., 'OSD-48-968c...'), or a list of assay identifiers to pool. When a list is passed, results are pooled across the methylation assays and the output gains an `assay_id` column so each row's source is identifiable; the same region significant in two pooled assays appears as two rows.
- `top_n` (integer or null, optional): How many regions to display for each of the hyper- and hypo-methylated lists, default: 10. Pass `null` (or omit) to return ALL rows passing the filters. When the full set is large (>100 rows), the inline markdown table is capped at the first 100 rows while the CSV contains every row.
- `q_value_threshold` (number, optional): q-value threshold for significance, default: 0.05. Only regions with `q_value <= this value` are returned.
- `methylation_diff_threshold` (number, optional): Minimum |methylation_diff| magnitude in percentage points required for a row to be kept, default: 0.0 (any change). Hypermethylated rows must have `methylation_diff > threshold`; hypomethylated rows must have `methylation_diff < -threshold`.
- `in_promoter` (boolean or null, optional): MethylationRegion location filter. `True` returns only regions overlapping a gene promoter; `False` excludes promoter-overlapping regions; `null` (default) imposes no promoter filter. Applied during the Cypher query, so `top_n` and the "showing N of TOTAL" counts reflect the filtered universe.
- `in_exon` (boolean or null, optional): Location filter for exon-overlapping regions (`True` / `False` / `null`). Combines with other filters via AND.
- `in_intron` (boolean or null, optional): Location filter for intron-overlapping regions (`True` / `False` / `null`). Combines with other filters via AND.
- `dist_to_feature_max` (integer or null, optional): When set, return only regions whose `dist_to_feature` (bp to the nearest annotated gene feature) is `<= this value`. `null` (default) imposes no distance filter.

**Returns:**
- Markdown-formatted tables of the top-N (or all) hypermethylated and hypomethylated regions, including region identifiers, methylation differences, q-values, and location annotations; an `assay_id` column is added when pooling multiple assays.

### `find_differentially_abundant_organisms`

Returns the top-N organisms with increased and decreased abundance for a given assay. Works for both DESeq2 and ANCOM-BC abundance assays in a method-aware way.

**Parameters:**
- `assay_id` (string, required): Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')
- `top_n` (integer or null, optional): How many organisms to display for each of the increased- and decreased-abundance lists, default: 10. Pass `null` (or omit) to return ALL organisms passing the magnitude + significance filters.
- `adj_p_threshold` (number, optional): Adjusted p-value threshold for DESeq2 abundance assays, default: 0.05. Applied to rows with `adj_p_value` populated.
- `q_value_threshold` (number, optional): q-value threshold for ANCOM-BC abundance assays, default: 0.05. Applied to rows with `q_value` populated.
- `log2fc_threshold` (number, optional): Minimum |log2fc| magnitude required for a row to be kept, default: 0.0 (any change). Applies to BOTH DESeq2 and ANCOM-BC rows since log2fc is populated for both methods.
- `lnfc_threshold` (number or null, optional): Optional minimum |lnfc| magnitude. Only applied to rows with `lnfc` populated (ANCOM-BC); DESeq2 rows are not filtered by this parameter. Leave unset (`null`) to skip lnfc filtering entirely.

**Returns:**
- Markdown-formatted tables of the top-N (or all) organisms with increased and decreased abundance, including organism names, fold-change values, and the relevant significance statistics for each method

---

## Cross-assay analyses

### `find_common_differentially_expressed_genes`

Finds common differentially expressed genes across multiple assays, keeping up- and down-regulated directions separate.

**Parameters:**
- `assay_ids` (array of strings, required): List of assay identifiers to compare (e.g., ['OSD-253-abc123', 'OSD-253-def456'])
- `log2fc_threshold` (number, optional): Log2 fold change threshold for filtering genes, default: 1.0 (represents 2-fold change)
- `adj_p_threshold` (number, optional): Adjusted p-value threshold for significance, default: 0.05 (max value: 0.1)

**Returns:**
- Markdown-formatted tables showing:
  - Common upregulated genes across all assays with log2fc values for each assay
  - Common downregulated genes across all assays with log2fc values for each assay

**Process:**
1. Gets ALL genes with |log2fc| > threshold and adj_p_value < adj_p_threshold for each assay
2. Performs an inner join among upregulated genes and among downregulated genes
3. Returns genes that are differentially expressed in the same direction across all assays

### `find_common_differentially_methylated_regions`

Intersects differentially methylated regions across multiple assays, with optional MethylationRegion location and distance filters.

**Parameters:**
- `assay_ids` (array of strings, required): List of assay identifiers for methylation comparisons
- `methylation_diff_threshold` (number, optional): Methylation difference threshold, default: 0.0 (any change)
- `q_value_threshold` (number, optional): q-value threshold for significance, default: 0.05
- `in_promoter` (boolean or null, optional): MethylationRegion location filter. `True` restricts to regions overlapping a gene promoter; `False` excludes them; `null` (default) imposes no promoter filter. Passing `in_promoter=True` is the correct way to ask "which regions are commonly hypermethylated IN THE PROMOTER across these assays".
- `in_exon` (boolean or null, optional): Location filter for exon-overlapping regions (`True` / `False` / `null`).
- `in_intron` (boolean or null, optional): Location filter for intron-overlapping regions (`True` / `False` / `null`).
- `dist_to_feature_max` (integer or null, optional): When set, restrict to regions with `dist_to_feature <= this value` (bp). `null` (default) imposes no distance filter.

**Returns:**
- Markdown-formatted tables of methylation regions differentially methylated in the same direction (hyper/hypo) across all assays, with per-assay values

### `find_common_differentially_abundant_organisms`

Intersects differentially abundant organisms across multiple assays (e.g., comparing DESeq2 and ANCOM-BC methods).

**Parameters:**
- `assay_ids` (array of strings, required): List of assay identifiers for abundance comparisons (e.g., different methods like DESeq2, ANCOM-BC)
- `log2fc_threshold` (number, optional): Minimum |log2fc| magnitude for filtering organisms, default: 0.0 (any change). Applied to BOTH DESeq2 and ANCOM-BC rows since log2fc is populated for both methods.
- `q_value_threshold` (number, optional): q-value threshold for ANCOM-BC abundance assays, default: 0.05. Applied to rows with `q_value` populated.
- `adj_p_threshold` (number, optional): Adjusted p-value threshold for DESeq2 abundance assays, default: 0.05. Applied to rows with `adj_p_value` populated.
- `lnfc_threshold` (number or null, optional): Optional minimum |lnfc| magnitude. Only applied to rows with `lnfc` populated (ANCOM-BC); DESeq2 rows are not filtered by this parameter. Leave unset (`null`) to skip lnfc filtering entirely.

**Returns:**
- Markdown-formatted tables of organisms differentially abundant in the same direction across all assays, with per-assay values

### `find_common_de_genes_overlapping_dm_regions`

Identifies differentially expressed genes whose methylation region (e.g., promoter) is also differentially methylated in matched assays. Supports method-aware significance filtering and pooled-DM evidence across multiple methylation assays.

**Parameters:**
- `expression_assay_id` (string, required): Assay identifier for differential expression data
- `methylation_assay_id` (string or array of strings, required): Assay identifier for differential methylation data, or a list of identifiers to pool. When a list is passed, methylation evidence is unioned across the assays — a gene counts as (e.g.) hypermethylated if ANY pooled assay passes the threshold for it — before being intersected with the DE side.
- `log2fc_threshold` (number, optional): Log2 fold change threshold for DE genes, default: 1.0
- `adj_p_threshold` (number, optional): Adjusted p-value threshold for DE genes, default: 0.05
- `methylation_diff_threshold` (number, optional): Methylation diff threshold for DM regions, default: 0.0
- `q_value_threshold` (number, optional): q-value threshold for DM regions, default: 0.05
- `in_promoter` (boolean or null, optional): MethylationRegion location filter applied to the DM side of the overlap. When `True`, only DM regions overlapping a promoter contribute to the methylated-gene set — use this for classical epigenetic-silencing questions ("which downregulated genes have hypermethylation in their promoter?").
- `in_exon` (boolean or null, optional): Location filter for exon-overlapping regions (`True` / `False` / `null`).
- `in_intron` (boolean or null, optional): Location filter for intron-overlapping regions (`True` / `False` / `null`).
- `dist_to_feature_max` (integer or null, optional): When set, only DM regions with `dist_to_feature <= this value` (bp) contribute to the methylated-gene set. `null` (default) imposes no distance filter.

**Returns:**
- Markdown-formatted tables coupling differentially expressed genes with overlapping differentially methylated regions, reporting expression and methylation statistics side by side

---

## Cypher fallback

### `query`

Executes a read-only Cypher query on the Neo4j database. This is the fallback for questions the specialist tools don't cover — the `DEFAULT_INSTRUCTIONS` policy directs the LLM to prefer specialist tools first.

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

**Note:** Only read queries (e.g., MATCH) are allowed. Write queries (MERGE, CREATE, SET, DELETE, REMOVE, ADD, DROP) are rejected by a regex-based write filter before execution, and all sessions additionally run in Bolt-level `READ_ACCESS` mode.

---

## Plot generation, delivery, and saving

### `create_volcano_plot`

Creates a volcano plot for differential expression, methylation, or abundance data from a given assay. The PNG is returned inline and registered for resource fetch.

**Parameters:**
- `assay_id` (string, required): Assay identifier (e.g., 'OSD-253-6c5f9f37b9cb2ebeb2743875af4bdc86')
- `data_type` (string, optional): Which kind of differential data to plot, default: 'expression'. Options: 'expression' (differentially expressed genes), 'methylation' (differentially methylated regions), 'abundance' (differentially abundant organisms; works for both DESeq2 and ANCOM-BC).
- `log2fc_threshold` (number, optional): Log2 fold change threshold, default: 1.0 (= 2-fold). Applies to `data_type='expression'` and `data_type='abundance'`. Ignored for `data_type='methylation'`.
- `methylation_diff_threshold` (number, optional): Methylation difference threshold in percentage points, default: 10.0 (= |diff| > 10%). Applies only to `data_type='methylation'`. Ignored for other data types.
- `adj_p_threshold` (number, optional): Adjusted p-value (DESeq2) / q-value (ANCOM-BC, methylation) threshold for significance, default: 0.05. For abundance, both fields are checked in a method-aware way.
- `top_n` (integer, optional): How many significant points to label in the plot, selected by smallest p/q-value, default: 20
- `figsize_width` (integer, optional): Figure width in inches, default: 8
- `figsize_height` (integer, optional): Figure height in inches, default: 5
- `label_avoid_overlap` (boolean, optional): If `True`, use adjustText to reposition labels to avoid overlap, default: True. Disable on very large assays for faster rendering.

**Returns:**
- The generated volcano plot PNG, returned inline as image content and registered in the plot registry (retrievable via `fetch_plot` or the `plot://` resource)
- A Markdown-formatted summary with:
  - Study information
  - Factor comparison details
  - Thresholds used
  - Count statistics for significant features (total, up/hyper/increased, down/hypo/decreased, not significant)
- The "Save" block in the response includes the suggested filename to use with `fetch_plot` / `get_save_script`

**Visualization:**
- X-axis: log2 fold change (or methylation difference for methylation)
- Y-axis: -log10(adjusted p-value / q-value)
- Color coding:
  - Red: upregulated / hypermethylated / increased (positive change passing thresholds)
  - Blue: downregulated / hypomethylated / decreased (negative change passing thresholds)
  - Gray: not significant
- Top-N significant features are labeled

### `create_venn_diagram`

Creates Venn diagrams comparing differentially expressed genes (or DMRs, or DA organisms) between 2 or 3 assays. Also supports an `expression_methylation` variant comparing DE genes against DM genes. The PNG is returned inline and registered for resource fetch.

**Parameters:**
- `assay_id_1` (string, required): First assay identifier
- `assay_id_2` (string, required): Second assay identifier
- `assay_id_3` (string or null, optional): Third assay identifier for a 3-way Venn diagram, default: null
- `data_type` (string, optional): Type of data to compare, default: 'expression'. Options: 'expression' (differentially expressed genes), 'methylation' (differentially methylated genes), 'abundance' (differentially abundant organisms), 'expression_methylation' (overlap between DE genes and DM genes across assays).
- `log2fc_threshold` (number, optional): Log2 fold change threshold for filtering genes (used for expression and abundance data types), default: 1.0
- `methylation_diff_threshold` (number, optional): Methylation difference threshold for filtering regions (used for the methylation data type), default: 0.0 (any change)
- `adj_p_threshold` (number, optional): Adjusted p-value threshold, default: 0.05. Applied to expression assays and to DESeq2 abundance rows.
- `q_value_threshold` (number, optional): q-value threshold, default: 0.05. Applied to methylation assays and to ANCOM-BC abundance rows.
- `lnfc_threshold` (number or null, optional): Optional minimum |lnfc| magnitude for the 'abundance' data type. Only applied to rows with `lnfc` populated (ANCOM-BC); DESeq2 rows are not filtered. Ignored for non-abundance data types. Leave unset (`null`) to skip lnfc filtering.
- `direction_pair` (string, optional): For `data_type='expression_methylation'` only — which directional overlap(s) to render, default: 'all'. 'all' renders a 2×2 grid of the four biologically meaningful combinations (hypermethylated+downregulated, hypomethylated+upregulated, hypermethylated+upregulated, hypomethylated+downregulated). Specify one of 'hyper_down', 'hypo_up', 'hyper_up', 'hypo_down' to render only that single overlap. Ignored for other data types.
- `in_promoter` (boolean or null, optional): MethylationRegion location filter (`data_type='methylation'` or `'expression_methylation'` only). `True` includes only regions overlapping a gene promoter; `False` excludes them; `null` (default) imposes no filter.
- `in_exon` (boolean or null, optional): MethylationRegion location filter for exon-overlapping regions (`data_type='methylation'` or `'expression_methylation'` only).
- `in_intron` (boolean or null, optional): MethylationRegion location filter for intron-overlapping regions (`data_type='methylation'` or `'expression_methylation'` only).
- `dist_to_feature_max` (integer or null, optional): MethylationRegion distance filter (`data_type='methylation'` or `'expression_methylation'` only). When set, includes only regions whose `dist_to_feature` (bp to the nearest gene feature) is `<= this value`. `null` (default) imposes no distance filter.
- `figsize_width` (integer, optional): Figure width in inches, default: 10
- `figsize_height` (integer, optional): Figure height in inches, default: 6

**Returns:**
- The generated Venn diagram PNG, returned inline as image content and registered in the plot registry (retrievable via `fetch_plot` or the `plot://` resource)
- A Markdown-formatted summary with:
  - Study information
  - Assay comparisons (factor combinations)
  - Overlap statistics for each direction
- The "Save" block in the response includes the suggested filename to use with `fetch_plot` / `get_save_script`

**Visualization:**
- For `expression`, `methylation`, and `abundance`: side-by-side Venn diagrams for each direction (e.g., upregulated vs. downregulated), supporting 2-way or 3-way comparisons with color-coded assay legends
- For `expression_methylation`: a 2×2 grid (or a single panel when `direction_pair` is specified) showing overlap between DE genes and DM genes
- Consistent color scheme across diagrams; overlaps use blended colors

**Statistics Returned:**
- For 2-way: only in assay 1, only in assay 2, common to both
- For 3-way: only in each assay, pairwise overlaps, all-three overlap, totals per assay

### `fetch_plot`

Re-fetches the canonical PNG bytes of a previously generated plot from the in-memory registry. Safe to call repeatedly — it performs no Cypher, no matplotlib, and no re-render — so a failed fetch can be retried without re-running the analysis.

**Parameters:**
- `filename` (string, required): Suggested filename of a previously generated plot (e.g., 'venn_expression_2way_OSD-244.png'). The filename appears in the "Save" block of the most recent `create_volcano_plot` or `create_venn_diagram` response. The server keeps the last 8 plots in memory (FIFO eviction).

**Returns:**
- The plot's PNG bytes as embedded image content, so clients can render it inline
- An error message if the requested filename is not in the registry

### `get_save_script`

Returns guidance for saving a previously generated plot to the user's machine. With no filename, lists the plots currently in the registry; with a filename, returns the detailed save options for that plot.

**Parameters:**
- `filename` (string or null, optional): Suggested filename of a recently generated plot (e.g., 'volcano_plot_OSD-244_expression_30_days_vs_60_days.png'). The filename appears in the "Save" block of the most recent `create_volcano_plot` or `create_venn_diagram` response. Omit this parameter (`null`) to list the filenames of all plots currently available in the registry.

**Returns:**
- A markdown block with multiple save options for the plot: right-click save, ask the LLM client to save it, or fetch it via the `plot://` resource URI
- When `filename` is omitted, a list of all currently registered plot filenames; when a filename is not found, a message indicating it is not in the registry

### `plot://{filename}` (resource)

MCP resource template for retrieving the PNG bytes of a generated plot. Decoupled from the tool response that produced the plot, so a failed fetch can be retried without re-running the analysis.

**Type:** MCP resource (read via `resources/read`)

**URI:** `plot://<suggested_filename>` (e.g., `plot://venn_expression_2way_OSD-244.png`)

**Returns:**
- The raw PNG bytes for the named plot from the in-memory registry (last 8 plots)

---

## Output paths

### `set_output_directory`

Sets the user-facing directory where output files (volcano plots, Venn diagrams, CSV exports) should be saved for this session.

**Parameters:**
- `path` (string, required): Absolute path on the USER's local machine where output files should be saved. Examples: '/Users/jane/Downloads', '/home/jane/Downloads', 'C:/Users/Jane/Downloads' (forward slashes work on Windows too with pathlib). Avoid trailing path separators.

**Returns:**
- A confirmation message echoing the directory that was set, or an error message if the path is empty/invalid

### `get_output_directory`

Returns the currently configured output directory for this session.

**Parameters:**
- None

**Returns:**
- The path previously set by `set_output_directory`, or a note indicating that no output directory has been configured (pointing the user to `set_output_directory`)

---

## Mermaid & transcript utilities

### `clean_mermaid_diagram`

Cleans a Mermaid class diagram by removing unwanted elements so it renders correctly.

**Parameters:**
- `mermaid_content` (string, required): The raw Mermaid class diagram content

**Returns:**
- Cleaned Mermaid content with unwanted elements removed

**Cleaning Operations:**
- Removes all note statements that would render as unreadable yellow boxes
- Removes empty curly braces from class definitions
- Truncates strings after newline characters (e.g., "ClassName\nextra" becomes "ClassName")
- Removes vertical bars (|), which are not allowed in class diagrams

**Use Case:**
- Use this tool to clean up Mermaid diagrams before rendering to ensure proper visualization (see the `visualize_schema` workflow)

### `create_chat_transcript`

Provides a prompt template for creating a chat transcript in markdown format.

**Parameters:**
- None

**Returns:**
- A markdown template for documenting conversations with user prompts and assistant responses, including instructions to embed any generated plots inline as image references

**Template Structure:**
```markdown
## Chat Transcript
<Title>

👤 **User**  
<prompt>

---

🧠 **Assistant**  
<entire text response goes here>

*Created by mcp-genelab {version} using {model_string} on {date}*
```

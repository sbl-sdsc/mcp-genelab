# Chat Transcript — GeneLab Knowledge Graph: Assay Node & Differential Expression Edge (OSD-104)

👤 **User**

@mcp-genelab: describe the assay node and its properties, and include a spaceflight vs ground control comparison from OSD-104 as an example

---

<details>
<summary>🧠 Reasoning / tool calls</summary>

- Loaded the genelab schema tools via `tool_search`.
- Called `get_neo4j_schema` to retrieve the `Assay` node properties.
- Called `get_study_info(study_id="OSD-104")` to identify the study's assays. OSD-104 = "Rodent Research 1" (SpaceX-4), *Mus musculus*, soleus muscle, two RNA-Seq / DESeq2 assays (one oriented Space Flight vs Ground Control, the reverse for the other).
- Called `find_differentially_expressed_genes(assay_id="OSD-104-ec6e344401cc5008b3cb12c08cad9c7f", top_n=10)` to pull the spaceflight vs ground control example data (2,107 up / 2,520 down at adj. p ≤ 0.05).

</details>

🧠 **Assistant**

## The Assay node

In the GeneLab knowledge graph, an **Assay** represents a single differential-analysis comparison within a study — a specific measurement type run on a specific pair of experimental conditions, with a defined analysis method. Studies connect to assays via `(Study)-[:PERFORMED_SpAS]->(Assay)`, and each assay holds the differential results as edges to feature nodes (`MGene`, `MethylationRegion`, or `Organism`).

### Properties

| Property | Meaning |
|----------|---------|
| `identifier` | Unique assay ID (e.g. `OSD-104-ec6e344401cc5008b3cb12c08cad9c7f`) |
| `name` | Human-readable assay name |
| `technology` | Platform used (e.g. RNA Sequencing, WGBS, amplicon sequencing) |
| `measurement` | What is measured (e.g. transcription profiling, DNA methylation profiling) |
| `differential_analysis_method` | Statistical method (e.g. DESeq2, methylKit, ANCOM-BC) |
| `factors_1` / `factors_2` | The two experimental conditions being contrasted (e.g. Space Flight vs Ground Control) |
| `material_1` / `material_2` | Source material/tissue for each condition |
| `material_id_1` / `material_id_2` | Material identifiers |
| `material_name_1` / `material_name_2` | Material display names |
| `factor_space_1` / `factor_space_2` | The factor-space encoding for each condition |

The factor/material fields are duplicated (`_1` / `_2`) because an assay encodes a directional comparison: condition 1 versus condition 2. The `differential_analysis_method` is significant because it changes which statistics live on the result edges (DESeq2 → `adj_p_value`; ANCOM-BC → `q_value` plus `lnfc`).

### Outgoing differential-result edges

Depending on its `measurement`, an assay points to one feature type, with the statistics stored on the edge itself:

- `-[:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(MGene)` — carries `log2fc`, `adj_p_value`, group means/stdevs
- `-[:MEASURED_DIFFERENTIAL_METHYLATION_ASmMR]->(MethylationRegion)` — carries `methylation_diff`, `q_value`
- `-[:MEASURED_DIFFERENTIAL_ABUNDANCE_ASmO]->(Organism)` — carries `log2fc`/`lnfc`, `adj_p_value`/`q_value`

## Example: Spaceflight vs Ground Control in OSD-104

OSD-104 ("Rodent Research 1," SpaceX-4) has two RNA-Seq / DESeq2 assays on mouse soleus muscle. The assay `OSD-104-ec6e344401cc5008b3cb12c08cad9c7f` encodes the comparison with `factors_1` = **Space Flight** and `factors_2` = **Ground Control** (`material_1`/`material_2` = Soleus-both sides). Reading its differential-expression edges gives the spaceflight effect: 2,107 genes upregulated and 2,520 downregulated (adj. p ≤ 0.05).

Top upregulated in spaceflight (vs ground control):

| Gene | Log2FC | Adj. p | Mean SF | Mean GC |
|------|--------|--------|---------|---------|
| Krt17 | 21.87 | 1.4e-11 | 34.55 | 1.00 |
| Krt25 | 8.35 | 0.025 | 53.35 | 1.16 |
| Krt14 | 8.30 | 0.026 | 63.42 | 1.22 |
| Krt71 | 7.87 | 0.0039 | 80.21 | 1.33 |
| Krt5 | 7.45 | 0.011 | 110.99 | 1.63 |

Top downregulated in spaceflight:

| Gene | Log2FC | Adj. p | Mean SF | Mean GC |
|------|--------|--------|---------|---------|
| Gcat | -6.45 | 4.1e-6 | 1.00 | 18.02 |
| Gm23925 | -5.53 | 0.0002 | 1.00 | 9.97 |
| Ppp1r1c | -3.89 | 3.3e-8 | 3.26 | 23.64 |
| Dhrs9 | -3.74 | 1.9e-15 | 3.95 | 40.65 |
| Unc13d | -3.72 | 9.7e-41 | 20.96 | 239.39 |

The striking up-regulation of keratin genes (Krt17/25/14/71/5/35/75) in flight is a clear signature in this assay's data. Note the sibling assay `OSD-104-f6e05c41...` is the reverse-direction comparison (Ground Control vs Space Flight) — same underlying data, flipped contrast — which is why `factors_1`/`factors_2` matter when picking the right assay.

---

👤 **User**

@mcp-genelab: describe the differential expression edge and its properties, and include a spaceflight vs ground control comparison from OSD-104 as an example

---

<details>
<summary>🧠 Reasoning / tool calls</summary>

No new tool calls were needed — the edge schema (`MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG` properties) and the OSD-104 spaceflight vs ground control data were already retrieved in the previous turn and reused directly.

</details>

🧠 **Assistant**

## The differential expression edge

In the GeneLab knowledge graph, differential gene expression is stored on the relationship `MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG`, directed as:

```
(Assay)-[:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]->(MGene)
```

Each edge represents one gene's differential-expression result for one assay's comparison. The statistics live **on the edge itself**, not on the nodes — so the same gene can carry different log2fc/p-values across different assays, and a single assay fans out to one edge per measured gene.

### Edge properties

| Property | Meaning |
|----------|---------|
| `log2fc` | Log2 fold change of condition 1 vs condition 2 (positive = up in `factors_1`, negative = up in `factors_2`) |
| `adj_p_value` | Benjamini-Hochberg adjusted p-value (DESeq2); significance threshold typically ≤ 0.05 |
| `group_mean_1` | Mean normalized expression in condition 1 (`factors_1`) |
| `group_stdev_1` | Standard deviation in condition 1 |
| `group_mean_2` | Mean normalized expression in condition 2 (`factors_2`) |
| `group_stdev_2` | Standard deviation in condition 2 |

The direction of `log2fc` is tied to the assay's `factors_1`/`factors_2` orientation, which is why OSD-104 has two mirror assays (one Space Flight vs Ground Control, one the reverse) — the same gene's edge flips sign between them.

## Example: Spaceflight vs Ground Control in OSD-104

Assay `OSD-104-ec6e344401cc5008b3cb12c08cad9c7f` (RNA-Seq, DESeq2, mouse soleus) is oriented `factors_1` = Space Flight, `factors_2` = Ground Control. Reading its expression edges yields 2,107 significantly upregulated and 2,520 downregulated genes (adj. p ≤ 0.05). Each row below is one `MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG` edge, showing how the properties populate:

Top upregulated in spaceflight (high positive `log2fc`):

| Gene (→MGene) | log2fc | adj_p_value | group_mean_1 (SF) | group_mean_2 (GC) |
|------|--------|-------------|---------|---------|
| Krt17 | 21.87 | 1.4e-11 | 34.55 | 1.00 |
| Krt25 | 8.35 | 0.025 | 53.35 | 1.16 |
| Krt14 | 8.30 | 0.026 | 63.42 | 1.22 |
| Krt71 | 7.87 | 0.0039 | 80.21 | 1.33 |
| Krt5 | 7.45 | 0.011 | 110.99 | 1.63 |

Top downregulated in spaceflight (negative `log2fc`):

| Gene (→MGene) | log2fc | adj_p_value | group_mean_1 (SF) | group_mean_2 (GC) |
|------|--------|-------------|---------|---------|
| Gcat | -6.45 | 4.1e-6 | 1.00 | 18.02 |
| Gm23925 | -5.53 | 0.0002 | 1.00 | 9.97 |
| Ppp1r1c | -3.89 | 3.3e-8 | 3.26 | 23.64 |
| Dhrs9 | -3.74 | 1.9e-15 | 3.95 | 40.65 |
| Unc13d | -3.72 | 9.7e-41 | 20.96 | 239.39 |

You can read the edge directly from the means: for Krt17, `group_mean_1` (34.55, Space Flight) vastly exceeds `group_mean_2` (1.00, Ground Control), giving a large positive `log2fc`; for Unc13d the means reverse (20.96 vs 239.39), giving a strongly negative `log2fc` with an extremely small `adj_p_value` (9.7e-41).

---

*Created by [mcp-genelab](https://github.com/sbl-sdsc/mcp-genelab) 0.5.0 on 2026-06-25*

Model: claude-opus-4-8

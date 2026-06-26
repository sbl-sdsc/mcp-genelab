# Chat Transcript — Spaceflight Gene Expression & Disease Relevance (OSD-161)

**Objective:** Integrate GeneLab and SPOKE knowledge to characterize spaceflight-associated gene expression changes and their disease relevance.

---

## User Prompt (verbatim)

> Objective: Integrate GeneLab and SPOKE knowledge to characterize spaceflight-associated gene expression changes and their disease relevance.
>
> **@mcp-genelab:**
> 1. Retrieve study OSD-161 and summarize its experimental design (organism, mission, spaceflight vs. ground control).
> 2. List all assays associated with this study.
> 3. Extract the unique experimental factors across these assays (e.g., tissue, timepoint, platform).
> 4. Compare assay for spaceflight vs. ground control and return:
>    * Top 10 up-regulated genes
>    * Top 10 down-regulated genes (include log fold change, adjusted p-value, and group means and standard deviations if available).
> 5. Map the resulting genes to their human orthologs
>
> **@spoke-okn:**
> 6. Map the human orthologs to associated diseases, indicating:
>    * Disease name
>    * Source
>    * Number of supporting associations (if available).
>
> **@mcp-genelab:**
> 7. Create a verbatim chat transcript, and include the reasoning sections as collapsible sections in the transcript

---

## 1. Study OSD-161 — Experimental Design

<details>
<summary><strong>Reasoning</strong></summary>

Called `get_study_info(study_id="OSD-161")` rather than a raw Cypher query, since study-metadata questions are exactly what the specialist tool is built for (it returns formatted metadata + an assay inventory + an inline CSV side-channel).
</details>

**Project:** Rodent Research-3 (RR-3) — Spaceflight Study, sponsored by Eli Lilly and Co. and the Center for the Advancement of Science in Space.

**Organism:** *Mus musculus* (BALB/c, 12-week-old female mice; NCBI Taxonomy 10090).

**Mission:** SpaceX-8 (CRS-8) to the ISS.

**Design:** Twenty BALB/c female mice (ten control, ten treated) were flown to the ISS and housed in the Rodent Habitat for 39–42 days. Twenty comparison mice of similar age/sex/strain were maintained as ground controls. The study was designed to test a countermeasure against spaceflight-induced muscle and bone loss. The GeneLab knowledge graph holds the RNA-Seq transcription-profiling assays for this study, contrasting **Space Flight**, **Ground Control**, and **Basal Control** conditions in adrenal gland tissue.

---

## 2. Assays Associated with OSD-161

<details>
<summary><strong>Reasoning</strong></summary>

The assay inventory is returned directly by `get_study_info`. All six assays are RNA-Seq, transcription profiling, DESeq2, on adrenal gland tissue. The six assays are the directional/pairwise contrasts among the three conditions (Space Flight, Ground Control, Basal Control).
</details>

| Assay ID | Technology | Measurement | Method | Factors 1 | Factors 2 | Tissue |
|----------|-----------|-------------|--------|-----------|-----------|--------|
| `OSD-161-10523b5f84754546b70f190496c2b64d` | RNA-Seq | transcription profiling | DESeq2 | Ground Control | Space Flight | Adrenal gland |
| `OSD-161-4445c36fd637949b0bddb5dca726843d` | RNA-Seq | transcription profiling | DESeq2 | Basal Control | Space Flight | Adrenal gland |
| `OSD-161-57522a86780f539b7c19500a8f626036` | RNA-Seq | transcription profiling | DESeq2 | Space Flight | Basal Control | Adrenal gland |
| `OSD-161-84752fd21368649ff52a4d70fdad83d8` | RNA-Seq | transcription profiling | DESeq2 | Basal Control | Ground Control | Adrenal gland |
| `OSD-161-9d67031a49dbfbf2353df453c8e992d1` | RNA-Seq | transcription profiling | DESeq2 | Ground Control | Basal Control | Adrenal gland |
| `OSD-161-b35ae45a8db5fd73b65f7aacba3b38ea` | RNA-Seq | transcription profiling | DESeq2 | Space Flight | Ground Control | Adrenal gland |

---

## 3. Unique Experimental Factors

<details>
<summary><strong>Reasoning</strong></summary>

Extracted from the factor columns across the six assays. The factor space collapses to three biological conditions and one tissue/platform; there is no timepoint axis (single terminal harvest after the 39–42 day flight).
</details>

- **Condition / factor levels:** Space Flight, Ground Control, Basal Control (pairwise contrasts among these make up the six assays).
- **Tissue / material:** Adrenal gland (all assays).
- **Platform / technology:** RNA Sequencing (RNA-Seq), transcription profiling.
- **Differential analysis method:** DESeq2.
- **Timepoint:** Single terminal collection (no timepoint factor in the KG).

---

## 4. Differential Expression — Space Flight vs. Ground Control

<details>
<summary><strong>Reasoning</strong></summary>

Selected assay `OSD-161-b35ae45a8db5fd73b65f7aacba3b38ea` (Factors 1 = Space Flight, Factors 2 = Ground Control) as the direct spaceflight-vs-ground-control contrast. Called `find_differentially_expressed_genes(assay_id=..., top_n=10)`, which applies the adj_p_value ≤ 0.05 filter and returns up/down lists with log2FC, adjusted p-value, and per-group means and standard deviations. Group 1 = Space Flight, Group 2 = Ground Control. The filter yielded only 5 significant upregulated genes and 11 downregulated (top 10 shown).
</details>

### Up-regulated (Space Flight > Ground Control) — all 5 passing adj-p ≤ 0.05

| Gene | Name | log2FC | Adj. p | Mean (SF) | SD (SF) | Mean (GC) | SD (GC) |
|------|------|-------:|-------:|----------:|--------:|----------:|--------:|
| Fos | FBJ osteosarcoma oncogene | 1.956 | 1.75e-02 | 6841.26 | 2414.35 | 1763.89 | 734.77 |
| Btg2 | BTG anti-proliferation factor 2 | 1.368 | 1.55e-08 | 9567.83 | 1495.77 | 3708.44 | 938.08 |
| Cited2 | Cbp/p300-interacting transactivator 2 | 0.917 | 1.38e-05 | 4891.05 | 615.57 | 2590.43 | 570.84 |
| Dusp1 | dual specificity phosphatase 1 | 0.758 | 1.24e-02 | 4809.28 | 1103.11 | 2845.37 | 394.28 |
| Gata6 | GATA binding protein 6 | 0.216 | 2.72e-02 | 5209.29 | 169.67 | 4484.32 | 214.34 |

### Down-regulated (Space Flight < Ground Control) — top 10 of 11

| Gene | Name | log2FC | Adj. p | Mean (SF) | SD (SF) | Mean (GC) | SD (GC) |
|------|------|-------:|-------:|----------:|--------:|----------:|--------:|
| Gm2436 | predicted gene 2436 | -23.079 | 2.66e-06 | 1.00 | 0.00 | 57.05 | 84.55 |
| H2-Q6 | histocompatibility 2, Q region locus 6 | -2.285 | 1.75e-02 | 103.24 | 15.34 | 499.46 | 512.95 |
| Gad1 | glutamate decarboxylase 1 | -1.164 | 4.74e-02 | 59.85 | 13.53 | 133.81 | 54.10 |
| Cd72 | CD72 antigen | -0.878 | 3.52e-02 | 148.55 | 24.29 | 272.06 | 87.40 |
| Cd74 | CD74 antigen (MHC class II invariant chain) | -0.726 | 4.74e-02 | 4427.39 | 367.55 | 7320.66 | 2700.19 |
| H2-Eb1 | histocompatibility 2, class II antigen E beta | -0.691 | 4.74e-02 | 1095.04 | 128.73 | 1767.98 | 508.15 |
| Oasl1 | 2'-5' oligoadenylate synthetase-like 1 | -0.686 | 1.75e-02 | 194.64 | 16.39 | 312.70 | 80.11 |
| H2-Ab1 | histocompatibility 2, class II antigen A, beta 1 | -0.659 | 3.74e-02 | 1064.42 | 113.51 | 1680.07 | 441.53 |
| H2bc8 | H2B clustered histone 8 | -0.461 | 9.67e-03 | 652.13 | 52.28 | 897.83 | 103.00 |
| Arrdc4 | arrestin domain containing 4 | -0.425 | 2.18e-02 | 2233.06 | 274.23 | 2998.18 | 379.24 |

*(An 11th downregulated gene exists below the top-10 cutoff.)*

---

## 5. Mouse → Human Ortholog Mapping

<details>
<summary><strong>Reasoning</strong></summary>

GeneLab identifies genes with mouse symbols; SPOKE-okn is human-centric and keys genes by Ensembl ID and human gene label. `get_join_strategy("spoke-genelab","spoke-okn")` confirmed no direct shared gene identifier (GeneLab uses NCBI Gene / GeneSymbol; spoke-okn uses Ensembl), with a bridge possible via gene-expression-atlas-okn. For these well-characterized genes the orthology is unambiguous and follows the standard HGNC/MGI ortholog convention (mouse symbol → uppercased human symbol). The mouse MHC (H2) genes map to the human HLA region: H2-Q6 → HLA class I (HLA-A/HLA-B), H2-Eb1 → HLA-DRB1, H2-Ab1 → HLA-DQB1. Two rows have no human ortholog: `Gm2436` (a predicted mouse gene) and the mouse-specific histocompatibility locus structure. Human orthologs were then confirmed to exist in spoke-okn by querying their labels directly.
</details>

| Mouse gene (GeneLab) | Direction | Human ortholog (SPOKE) | Note |
|----------------------|-----------|------------------------|------|
| Fos | up | FOS | 1:1 |
| Btg2 | up | BTG2 | 1:1 |
| Cited2 | up | CITED2 | 1:1 |
| Dusp1 | up | DUSP1 | 1:1 |
| Gata6 | up | GATA6 | 1:1 |
| Gm2436 | down | — | predicted gene, no human ortholog |
| H2-Q6 | down | HLA-A / HLA-B | MHC class I region |
| Gad1 | down | GAD1 | 1:1 |
| Cd72 | down | CD72 | 1:1 |
| Cd74 | down | CD74 | 1:1 |
| H2-Eb1 | down | HLA-DRB1 | MHC class II |
| Oasl1 | down | OASL | mouse Oasl1/Oasl2 → human OASL |
| H2-Ab1 | down | HLA-DQB1 | MHC class II |
| H2bc8 | down | H2BC8 (HIST1 cluster) | histone cluster |
| Arrdc4 | down | ARRDC4 | 1:1 |

---

## 6. Human Orthologs → Disease Associations (SPOKE-okn)

<details>
<summary><strong>Reasoning</strong></summary>

Queried spoke-okn over the `ASSOCIATES_DaG` (Disease–associates–Gene) predicate, filtering on the human ortholog labels. Disease nodes carry their vocabulary source via `dct:source` (Disease Ontology). The `ASSOCIATES_DaG` edge in this schema carries no per-edge numeric support/evidence-count property, so "number of supporting associations" is reported as the count of distinct associated diseases per gene (the degree of the gene in the disease–gene subgraph). Genes BTG2, CITED2, DUSP1, CD72, CD74, H2BC8, and ARRDC4 returned no disease associations in spoke-okn and are omitted. For the MHC genes, HLA-A and HLA-B (class I) and HLA-DRB1/HLA-DQB1 (class II) were used as the human orthologs.
</details>

**Source for all disease nodes:** Disease Ontology. **Association predicate:** `ASSOCIATES_DaG`. **"# supporting associations"** = number of distinct diseases linked to that gene.

### FOS (up) — 8 associations
anxiety disorder, cardiomyopathy, depressive disorder, diabetes mellitus, inflammatory bowel disease, liver disease, nervous system disease, polycystic ovary syndrome

### GATA6 (up) — 3 associations
diabetes mellitus, epilepsy, liver disease

### GAD1 (down) — 5 associations
autism spectrum disorder, depressive disorder, epilepsy, nervous system disease, schizophrenia

### OASL (down, ortholog of Oasl1) — 1 association
endometriosis

### HLA-A (down, MHC-I ortholog of H2-Q6) — 4 associations
alopecia areata, nervous system disease, rheumatoid arthritis, viral infectious disease

### HLA-B (down, MHC-I ortholog of H2-Q6) — 22 associations
asthma, bipolar disorder, cardiomyopathy, depressive disorder, dermatitis, encephalitis, endocarditis, epilepsy, gastroesophageal reflux disease, human immunodeficiency virus infectious disease, hypertension, inflammatory bowel disease, liver disease, major depressive disorder, meningitis, migraine, nervous system disease, pancreatitis, psoriasis, rheumatoid arthritis, skin benign neoplasm, viral infectious disease

### HLA-DRB1 (down, MHC-II ortholog of H2-Eb1) — 22 associations
alopecia areata, Alzheimer's disease, arteriosclerosis, asthma, depressive disorder, dermatitis, diabetes mellitus, gastroesophageal reflux disease, glaucoma, glomerulonephritis, hypertension, inflammatory bowel disease, interstitial nephritis, liver disease, meningitis, migraine, multiple sclerosis, nervous system disease, obesity, psoriasis, rheumatoid arthritis

### HLA-DQB1 (down, MHC-II ortholog of H2-Ab1) — 17 associations
alopecia areata, asthma, chronic obstructive pulmonary disease, depressive disorder, dermatitis, diabetes mellitus, epilepsy, gastroesophageal reflux disease, hypertension, inflammatory bowel disease, liver disease, migraine, obesity, oral cavity cancer, psoriasis, rheumatoid arthritis, schizophrenia

---

## Summary of Findings

<details>
<summary><strong>Reasoning / interpretation</strong></summary>

The spaceflight adrenal-gland signature in OSD-161 is dominated by (a) an immediate-early / stress-response upregulation (Fos, Btg2, Dusp1, Cited2) consistent with the adrenal stress axis under spaceflight, and (b) a coordinated downregulation of MHC class I and class II antigen-presentation machinery (H2-Q6, Cd74, H2-Eb1, H2-Ab1, Cd72) plus an interferon-response gene (Oasl1) — pointing to immune/antigen-presentation suppression. When mapped through human orthologs into SPOKE, the strongest disease connectivity comes from the HLA orthologs (immune, autoimmune, neuropsychiatric, and metabolic disease) and from FOS/GAD1 (neuropsychiatric and metabolic disease). This is association-level evidence (Disease Ontology curation), not causal evidence in spaceflight.
</details>

The spaceflight-vs-ground-control contrast in adrenal gland shows an immediate-early stress-response upregulation (FOS, BTG2, DUSP1, CITED2) alongside downregulation of MHC antigen-presentation genes (the H2 class I/II loci, CD74, CD72) and the interferon-response gene Oasl1. Mapping the human orthologs into SPOKE links these changes most strongly — via the HLA genes — to autoimmune, inflammatory, neuropsychiatric, and metabolic disease, with additional neuropsychiatric/metabolic connections through FOS and GAD1.

---

*Transcript generated from live GeneLab (mcp-genelab) and SPOKE (spoke-okn) knowledge-graph queries.*

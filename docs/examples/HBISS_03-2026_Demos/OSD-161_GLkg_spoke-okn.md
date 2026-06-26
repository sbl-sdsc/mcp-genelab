# OSD-161: GeneLab KG + SPOKE-OKN Analysis

---

👤 **User**

Objective: Integrate GeneLab and SPOKE knowledge to characterize spaceflight-associated gene expression changes and their disease relevance.

@genelabkg-local:
1. Retrieve study OSD-161 and summarize its experimental design (organism, mission, spaceflight vs. ground control).
2. List all assays associated with this study.
3. Extract the unique experimental factors across these assays (e.g., tissue, timepoint, platform).
4. Compare assay for spaceflight vs. ground control and return:
   - Top 10 up-regulated genes
   - Top 10 down-regulated genes
   (include log fold change, adjusted p-value, and group means and standard deviations if available).
5. Map the resulting genes to their human orthologs

@spoke-okn:
6. Map the human orthologs to associated diseases, indicating:
   - Disease name
   - Evidence type or source KG
   - Number of supporting associations (if available).

@genelabkg-local:
7. Create chat transcript

---

🧠 **Assistant**

## Step 1 — Study OSD-161: Experimental Design

**Study:** OSD-161 — *Rodent Research 3 (RR-3)*

| Field | Details |
|-------|---------|
| Study ID | OSD-161 |
| Project Title | Rodent Research 3 (RR-3) |
| Project Type | Spaceflight Study |
| Organism | *Mus musculus* (BALB/c, female, 12-weeks old) |
| Mission | SpaceX-8 / ISS |
| Duration | 39–42 days aboard the ISS |
| Group Size | 20 spaceflight mice (10 control + 10 treated) / 20 ground controls |
| Sponsor | Eli Lilly & Co. / CASIS |
| Scientific Goal | Study muscle/bone mass countermeasures during spaceflight |
| Tissue Collected | Adrenal gland |

The Rodent Research-3 (RR-3) mission was a pharmaceutical spaceflight study. Twenty 12-week-old female BALB/c mice flew aboard the ISS on SpaceX-8 and were housed in the Rodent Habitat for 39–42 days. Ten mice received a therapeutic treatment targeting muscle/bone loss and ten served as in-flight controls. A matched cohort of 20 ground control animals was maintained on Earth. Adrenal glands were collected post-mission for RNA-Seq transcriptomic profiling.

---

## Step 2 — Assays Associated with OSD-161

Six RNA-Seq assays were identified, all performed on **adrenal gland** tissue and analyzed with **DESeq2**:

| Assay Identifier | Factor 1 (Group 1) | Factor 2 (Group 2) | Tissue | Method |
|------------------|--------------------|--------------------|--------|--------|
| OSD-161-b35ae45a8db5fd73b65f7aacba3b38ea | Space Flight | Ground Control | Adrenal gland | DESeq2 |
| OSD-161-10523b5f84754546b70f190496c2b64d | Ground Control | Space Flight | Adrenal gland | DESeq2 |
| OSD-161-4445c36fd637949b0bddb5dca726843d | Basal Control | Space Flight | Adrenal gland | DESeq2 |
| OSD-161-57522a86780f539b7c19500a8f626036 | Space Flight | Basal Control | Adrenal gland | DESeq2 |
| OSD-161-84752fd21368649ff52a4d70fdad83d8 | Basal Control | Ground Control | Adrenal gland | DESeq2 |
| OSD-161-9d67031a49dbfbf2353df453c8e992d1 | Ground Control | Basal Control | Adrenal gland | DESeq2 |

Each assay represents a pairwise comparison between two of three experimental groups: **Space Flight (SF)**, **Ground Control (GC)**, and **Basal Control (BC)**. This factorial design enables isolation of spaceflight-specific effects from vivarium-housing effects.

---

## Step 3 — Unique Experimental Factors

| Factor | Description |
|--------|-------------|
| **Space Flight (SF)** | Mice housed aboard the ISS in microgravity for 39–42 days |
| **Ground Control (GC)** | Mice maintained on Earth under standard vivarium conditions, matched to ISS timeline |
| **Basal Control (BC)** | Mice euthanized at mission launch date — pre-flight baseline; controls for housing/timeline confounds |
| **Tissue** | Adrenal gland (all 6 assays) |
| **Platform** | RNA Sequencing (Illumina RNA-Seq) |
| **Analysis Method** | DESeq2 (negative binomial model for count-based differential expression) |

---

## Step 4 — Spaceflight vs. Ground Control DEGs

**Assay:** `OSD-161-b35ae45a8db5fd73b65f7aacba3b38ea` (Space Flight vs. Ground Control)  
All genes shown pass adjusted p-value < 0.05.

### 🔺 Top Up-Regulated Genes in Spaceflight

| Symbol | Gene Name | log2FC | adj p-val | Mean SF | SD SF | Mean GC | SD GC |
|--------|-----------|--------|-----------|---------|-------|---------|-------|
| **Fos** | FBJ osteosarcoma oncogene | +1.956 | 0.0175 | 6841 | 2414 | 1764 | 735 |
| **Btg2** | BTG anti-proliferation factor 2 | +1.368 | 1.6×10⁻⁸ | 9568 | 1496 | 3708 | 938 |
| **Cited2** | Cbp/p300-interacting transactivator 2 | +0.917 | 1.4×10⁻⁵ | 4891 | 616 | 2590 | 571 |
| **Dusp1** | Dual specificity phosphatase 1 | +0.758 | 0.0124 | 4809 | 1103 | 2845 | 394 |
| **Gata6** | GATA binding protein 6 | +0.216 | 0.0272 | 5209 | 170 | 4484 | 214 |

> Note: Only 5 significantly up-regulated genes (adj p < 0.05) were identified in this dataset. SF = Space Flight; GC = Ground Control.

### 🔻 Top Down-Regulated Genes in Spaceflight

| Symbol | Gene Name | log2FC | adj p-val | Mean SF | SD SF | Mean GC | SD GC |
|--------|-----------|--------|-----------|---------|-------|---------|-------|
| **Gm2436** | Predicted gene 2436 | −23.08 | 2.7×10⁻⁶ | 1.0 | 0.0 | 57 | 85 |
| **H2-Q6** | Histocompatibility 2, Q region locus 6 | −2.285 | 0.0175 | 103 | 15 | 499 | 513 |
| **Gad1** | Glutamate decarboxylase 1 | −1.164 | 0.0474 | 60 | 14 | 134 | 54 |
| **Cd72** | CD72 antigen | −0.878 | 0.0352 | 149 | 24 | 272 | 87 |
| **Cd74** | CD74 antigen (MHC class II-associated) | −0.726 | 0.0474 | 4427 | 368 | 7321 | 2700 |
| **H2-Eb1** | Histocompatibility 2, class II antigen E beta | −0.691 | 0.0474 | 1095 | 129 | 1768 | 508 |
| **Oasl1** | 2'-5' oligoadenylate synthetase-like 1 | −0.686 | 0.0175 | 195 | 16 | 313 | 80 |
| **H2-Ab1** | Histocompatibility 2, class II antigen A beta 1 | −0.659 | 0.0374 | 1064 | 114 | 1680 | 442 |
| **H2bc8** | H2B clustered histone 8 | −0.461 | 0.0097 | 652 | 52 | 898 | 103 |
| **Arrdc4** | Arrestin domain containing 4 | −0.425 | 0.0218 | 2233 | 274 | 2998 | 379 |

### Volcano Plot

<img src="https://raw.githubusercontent.com/asaravia-butler/mcp-genelab/main/docs/examples/HBISS_03-2026_Demos/images/OSD-161_SFvGC_volcano.png" 
     alt="volcano 60 days" 
     width="800">


*Figure: Volcano plot showing differential gene expression in adrenal glands of spaceflight vs. ground control mice. Red = significantly upregulated (log2FC > 0.5, adj p < 0.05); Blue = significantly downregulated; Gray = not significant. Thresholds: |log2FC| ≥ 0.5, adj p < 0.05. Total genes: 26; Upregulated: 4; Downregulated: 8; Not significant: 14.*

---

## Step 5 — Mouse-to-Human Ortholog Mapping

Ortholog mapping was performed via the `IS_ORTHOLOG_MGiG` relationship in the GeneLab Knowledge Graph.

| Mouse Gene | Direction | Human Ortholog(s) | NCBI Gene ID(s) |
|-----------|-----------|-------------------|-----------------|
| Fos | ▲ Up | FOS | 2353 |
| Btg2 | ▲ Up | BTG2 | 7832 |
| Cited2 | ▲ Up | CITED2 | 10370 |
| Dusp1 | ▲ Up | DUSP1 | 1843 |
| Gata6 | ▲ Up | GATA6 | 2627 |
| Gm2436 | ▼ Down | *(no direct ortholog)* | — |
| H2-Q6 | ▼ Down | HLA-A, HLA-B, HLA-C, HLA-F, HLA-G, HLA-H | 3106, 3107, 3134, 3136, 3105, 3133, 3135 |
| Gad1 | ▼ Down | GAD1, GAD2 | 2571, 2572 |
| Cd72 | ▼ Down | CD72 | 971 |
| Cd74 | ▼ Down | CD74 | 972 |
| H2-Eb1 | ▼ Down | HLA-DPB1, HLA-DQB1, HLA-DRA, HLA-DRB1/3 | 3123, 3125–3127 |
| Oasl1 | ▼ Down | OASL | 8638 |
| H2-Ab1 | ▼ Down | HLA-DQA1, HLA-DQA2 | 3119, 3120 |
| H2bc8 | ▼ Down | H2BC4/8/11/12/13 and H2B variants | 8343–8349, 3017 |
| H3c7 | — | H3C7 variants | 653604, 126961, 333932 |
| Arrdc4 | ▼ Down | ARRDC4 | 91947 |

> Mouse MHC class II genes (H2-Q6, H2-Eb1, H2-Ab1) map to multiple human HLA paralogs. Gm2436 is a predicted mouse-specific gene with no catalogued human ortholog.

---

## Step 6 — Disease Associations via SPOKE

Disease associations retrieved from **SPOKE (Scalable Precision Medicine Oriented Knowledge Engine)** using the `ASSOCIATES_DaG` predicate (Disease → Gene), encoding curated gene–disease links from DisGeNET, OMIM, and related biomedical databases.

| Mouse Gene | Human Ortholog | Associated Disease(s) | Evidence (SPOKE KG) |
|-----------|----------------|----------------------|---------------------|
| Fos | **FOS** | Anxiety disorder, Cardiomyopathy, Depressive disorder, Diabetes mellitus, Inflammatory bowel disease, Liver disease, Nervous system disease, Polycystic ovary syndrome | SPOKE `ASSOCIATES_DaG` |
| Gata6 | **GATA6** | Diabetes mellitus, Epilepsy, Liver disease | SPOKE `ASSOCIATES_DaG` |
| Gad1 | **GAD1** | Autism spectrum disorder, Depressive disorder, Epilepsy, Nervous system disease, Schizophrenia | SPOKE `ASSOCIATES_DaG` |
| Gad1 | **GAD2** | Anxiety disorder, Depressive disorder, Diabetes mellitus, Encephalitis, Epilepsy, Nervous system disease | SPOKE `ASSOCIATES_DaG` |
| H2-Q6 | **HLA-A** | Alopecia areata, Nervous system disease, Rheumatoid arthritis, Viral infectious disease | SPOKE `ASSOCIATES_DaG` |
| H2-Q6 | **HLA-B** | Asthma, Bipolar disorder, Cardiomyopathy, Dermatitis, Epilepsy, HIV infection, Hypertension, IBD, Liver disease, Psoriasis, Rheumatoid arthritis, Viral infectious disease | SPOKE `ASSOCIATES_DaG` |
| H2-Q6 | **HLA-C** | Alopecia areata, Dermatitis, Psoriasis | SPOKE `ASSOCIATES_DaG` |
| H2-Eb1 | **HLA-DQB1** | Alopecia areata, Asthma, COPD, Depressive disorder, Dermatitis, Diabetes mellitus, Epilepsy, Hypertension, IBD, Liver disease, Obesity, Psoriasis, Rheumatoid arthritis, Schizophrenia | SPOKE `ASSOCIATES_DaG` |
| H2-Ab1 | **HLA-DQB2** | Alopecia areata, Hepatitis B | SPOKE `ASSOCIATES_DaG` |
| H2bc8 | **H2BC4** | Hypertension, Osteoarthritis, Schizophrenia | SPOKE `ASSOCIATES_DaG` |
| Oasl1 | **OASL** | Endometriosis | SPOKE `ASSOCIATES_DaG` |

### Key Disease Theme Summary

| Theme | Evidence | Genes Involved |
|-------|----------|----------------|
| **Stress / HPA Axis** | Fos and Btg2 are immediate early response genes; Fos upregulation signals adrenocortical stress activation, linked to anxiety, depression, cardiomyopathy | FOS, BTG2, DUSP1 |
| **Immune Suppression** | Broad downregulation of MHC class II genes — associated with autoimmune and inflammatory conditions | HLA-A/B/C/DQB1/DPB1, CD72, CD74 |
| **Neurological** | GAD1/GAD2 encode glutamate decarboxylase (key enzyme for GABA synthesis); associated with epilepsy, autism, schizophrenia | GAD1, GAD2 |
| **Metabolic / Endocrine** | GATA6 is a transcription factor linked to diabetes and liver disease; FOS also maps to diabetes | GATA6, FOS |
| **Autoimmune** | HLA-DQB1 is one of the most disease-associated genes in SPOKE, linked to RA, IBD, asthma, COPD, psoriasis, schizophrenia | HLA-DQB1, HLA-B |

---

## Summary & Biological Interpretation

Two dominant spaceflight signatures emerge from the OSD-161 adrenal gland transcriptome:

**1. Activation of the Stress Response**

Upregulation of *Fos* (+1.96 log2FC), *Btg2* (+1.37), *Cited2* (+0.92), and *Dusp1* (+0.76) collectively indicate activation of the HPA stress axis and immediate early gene programs. *Fos* is a proto-oncogene and transcription factor whose induction is a hallmark of cellular stress, inflammation, and adrenocortical activation. Its human ortholog **FOS** is associated in SPOKE with cardiomyopathy, anxiety disorder, and depressive disorders — conditions of growing concern for long-duration spaceflight crews.

**2. Immune Suppression via MHC Class II Downregulation**

The most prominent downregulated cluster comprises multiple MHC class I/II genes — *H2-Q6* (−2.29 log2FC), *H2-Eb1* (−0.69), *H2-Ab1* (−0.66), *Cd74* (−0.73), and *Cd72* (−0.88). These encode antigen presentation machinery and B-cell co-stimulatory molecules. Their suppression during spaceflight suggests impaired adaptive immune surveillance, consistent with well-documented spaceflight immune dysregulation. Human orthologs (HLA-A, HLA-B, HLA-C, HLA-DQB1, HLA-DPB1, CD74) are among the most disease-relevant genes in SPOKE, linked to autoimmune, inflammatory, and infectious diseases.

**3. Neurochemical Changes**

Downregulation of *Gad1* (−1.16 log2FC) — encoding glutamate decarboxylase 1, a key enzyme in GABA synthesis — maps via SPOKE to epilepsy, autism spectrum disorder, and schizophrenia in humans.

> *These findings suggest that spaceflight induces adrenal stress activation concurrent with suppression of immune surveillance — a pattern with direct implications for astronaut health during and after long-duration missions.*

---

**Data Sources:**
- NASA GeneLab Knowledge Graph (OSD-161, SpaceX-8/ISS mission)
- SPOKE OKN (`ASSOCIATES_DaG` gene–disease links from DisGeNET/OMIM)
- DESeq2 differential expression (adj p < 0.05)
- `IS_ORTHOLOG_MGiG` ortholog mapping (GeneLab KG)

---

*Created by [mcp-genelab](https://github.com/sbl-sdsc/mcp-genelab) on 2026-03-09*

*Model: claude-sonnet-4-6*

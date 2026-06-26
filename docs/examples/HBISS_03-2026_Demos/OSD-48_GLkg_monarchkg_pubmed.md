# OSD-48: GeneLab KG + Monarch KG + PubMed Analysis

👤 **User**
Find datasets that have both differential expression and differential methylation

---

🧠 **Assistant**

I found **4 studies** in the GeneLab Knowledge Graph that have both differential gene expression and differential methylation data. Here's a summary:

## Studies with Both Differential Expression & Differential Methylation

All four studies involve **_Mus musculus_ (mouse)** as the organism.

| Study | Expression Assays | Methylation Assays | Notes |
|-------|:-----------------:|:------------------:|-------|
| **OSD-47** | 6 | 10 | Largest dataset |
| **OSD-48** | 12 | 24 | Most assays overall |
| **OSD-103** | 2 | 4 | — |
| **OSD-105** | 2 | 4 | — |

### Key Details:

- **OSD-47** — 6 expression assays + 10 methylation assays (Whole Genome Bisulfite Sequencing)
- **OSD-48** — 12 expression assays + 24 methylation assays (WGBS) — the richest multi-omic dataset
- **OSD-103** — 2 expression + 4 methylation assays
- **OSD-105** — 2 expression + 4 methylation assays

---

👤 **User**
Select assays for OSD-48

---

🧠 **Assistant**

Here are the available assay factor groups for **OSD-48**:

## Factor Arrays for OSD-48

| Index | Factors |
|-------|---------|
| 1 | Ground Control, Carcass |
| 2 | Ground Control, Upon euthanasia |
| 3 | Space Flight, Carcass |
| 4 | Space Flight, Upon euthanasia |

To create pairwise comparisons, select an **even number of indices** — consecutive pairs will be compared. For example:
- **`3,1`** → Space Flight Carcass **vs** Ground Control Carcass
- **`4,2`** → Space Flight Upon euthanasia **vs** Ground Control Upon euthanasia
- **`3,1,4,2`** → Both comparisons at once

Which indices would you like to select?

---

👤 **User**
3,1: Find hypermethylated genes in the promoter region, and find downregulated genes

---

🧠 **Assistant**

Here are the results for **OSD-48: Space Flight Carcass vs. Ground Control Carcass**:

## 🔬 Top Hypermethylated Promoter Regions (Space Flight vs. Ground Control)

Filtered for `in_promoter = TRUE`, `methylation_diff > 0`, sorted by methylation difference descending.

| Gene | Name | Chr | Methylation Diff | Q-value | SF Mean | GC Mean |
|------|------|-----|:----------------:|:-------:|:-------:|:-------:|
| **Magel2** | MAGE family member L2 | 7 | +58.9% | 9.7e-25 | 84.1% | 25.2% |
| **Nr4a3** | nuclear receptor subfamily 4A, member 3 | 4 | +45.8% | 4.6e-15 | 52.5% | 6.7% |
| **Mta1** | metastasis associated 1 | 12 | +37.2% | 7.1e-19 | 43.3% | 6.4% |
| **Epha2** | Eph receptor A2 | 4 | +36.5% | 2.6e-13 | 65.1% | 28.6% |
| **Stimate** | STIM activating enhancer | 14 | +35.5% | 8.0e-09 | 86.9% | 51.4% |
| **Lrrc8d** | leucine rich repeat containing 8D | 5 | +33.8% | 5.3e-09 | 69.5% | 35.5% |
| **Rtkn** | rhotekin | 6 | +33.2% | 8.1e-13 | 40.5% | 7.4% |
| **Scarb1** | scavenger receptor class B, member 1 | 5 | +33.1% | 2.4e-07 | 41.4% | 8.3% |
| **Hmga1** | high mobility group AT-hook 1 | 17 | +29.9% | 1.6e-12 | 42.7% | 13.0% |
| **Samhd1** | SAM domain and HD domain 1 | 2 | +25.9% | 1.1e-06 | 32.6% | 6.8% |

## 📉 Top Downregulated Genes (Space Flight vs. Ground Control)

Filtered for `log2fc < -1`, `adj_p < 0.05`, sorted by log2FC ascending.

| Gene | Name | log2FC | Adj. p-value |
|------|------|:------:|:------------:|
| **Apobec2** | apolipoprotein B mRNA editing enzyme 2 | -15.5 | 0.0018 |
| **Scp2-ps2** | sterol carrier protein 2, pseudogene 2 | -10.7 | 3.5e-08 |
| **Dbp** | D site albumin promoter binding protein | -4.7 | 9.6e-10 |
| **Per3** | period circadian clock 3 | -3.8 | 2.0e-07 |
| **Bhlhe41** | basic helix-loop-helix family member e41 | -3.1 | 0.0044 |
| **Wee1** | WEE1 homolog 1 | -2.3 | 1.6e-06 |
| **Ciart** | circadian associated repressor of transcription | -2.1 | 3.8e-04 |
| **Tef** | thyrotroph embryonic factor | -2.0 | 3.4e-07 |
| **Serpine1** | serine/cysteine peptidase inhibitor E1 | -1.7 | 0.0023 |
| **Gstt2** | glutathione S-transferase theta 2 | -1.6 | 1.3e-11 |
| **Acot4** | acyl-CoA thioesterase 4 | -1.5 | 1.6e-06 |

### Key Observations

**Circadian rhythm disruption** stands out strongly in the downregulated genes — *Dbp*, *Per3*, *Bhlhe41*, *Ciart*, and *Tef* are all core circadian clock components, consistently suppressed in spaceflight.

**Notable hypermethylated promoters** include *Nr4a3* (a nuclear receptor involved in stress response), *Mta1* (chromatin remodeling/metastasis), and *Epha2* (receptor tyrosine kinase) — genes whose silencing via promoter methylation in spaceflight could have broad downstream effects.

---

👤 **User**
Find the full overlap between hypermethylated promoter genes and downregulated genes

---

🧠 **Assistant**

Here is the **full overlap** — genes that are both hypermethylated in their promoter AND downregulated in spaceflight (Space Flight Carcass vs. Ground Control Carcass):

## 🔴 Overlap: Hypermethylated Promoter & Downregulated Genes

| Gene | Name | Promoter Methylation Diff | Meth Q-value | log2FC | Adj. p-value |
|------|------|:------------------------:|:------------:|:------:|:------------:|
| **Cux2** | cut-like homeobox 2 | +14.5% | 0.040 | -2.06 | 0.042 |
| **Pcsk4** | proprotein convertase subtilisin/kexin type 4 | +31.6% | 3.4e-10 | -1.21 | 2.3e-04 |
| **Tns2** | tensin 2 | +6.8% | 0.020 | -1.00 | 1.9e-04 |

Only **3 genes** show concordant promoter hypermethylation and transcriptional downregulation, suggesting that while spaceflight induces broad methylation changes, direct epigenetic silencing of specific genes is selective.

---

👤 **User**
Find the full overlap between hypermethylated promoter genes and downregulated genes (log2fc < -0.5, adj_p < 0.05)

---

🧠 **Assistant**

With the relaxed threshold (log2FC < -0.5, adj_p < 0.05), the overlap expands to **9 genes**:

## 🔴 Full Overlap: Hypermethylated Promoter & Downregulated Genes
**Space Flight Carcass vs. Ground Control Carcass (OSD-48)**

| Gene | Name | Methylation Diff | Meth Q-value | log2FC | Adj. p-value |
|------|------|:----------------:|:------------:|:------:|:------------:|
| **Cux2** | cut-like homeobox 2 | +14.5% | 0.040 | -2.06 | 0.042 |
| **Pcsk4** | proprotein convertase subtilisin/kexin type 4 | +31.6% | 3.4e-10 | -1.21 | 2.3e-04 |
| **Tns2** | tensin 2 | +6.8% | 0.020 | -1.00 | 1.9e-04 |
| **Acaa1a** | acetyl-CoA acyltransferase 1A | +4.4% | 0.032 | -0.93 | 8.0e-04 |
| **Per1** | period circadian clock 1 | +8.4% | 6.0e-04 | -0.84 | 3.8e-04 |
| **Tgm1** | transglutaminase 1 | +21.4% | 8.6e-04 | -0.69 | 0.046 |
| **Galk1** | galactokinase 1 | +13.8% | 7.0e-05 | -0.57 | 0.029 |
| **Fgfr2** | fibroblast growth factor receptor 2 | +2.8% | 0.0081 | -0.53 | 0.015 |
| **Gzf1** | GDNF-inducible zinc finger protein 1 | +15.2% | 0.0019 | -0.51 | 0.014 |

### Key Biological Themes

- **Circadian rhythm** — *Per1* joins the already-downregulated *Per3*, *Dbp*, *Ciart*, and *Tef*, and now shows concordant promoter hypermethylation
- **Metabolism** — *Acaa1a* (fatty acid β-oxidation) and *Galk1* (galactose metabolism) point to epigenetically-driven metabolic reprogramming
- **Signaling & growth** — *Fgfr2* and *Pcsk4* suggest dampened growth factor signaling
- **Cell adhesion & structure** — *Tns2* and *Tgm1* reflect disrupted mechanosensing
- **Transcription** — *Cux2* and *Gzf1* are transcription factors whose silencing could have broad downstream consequences

---

👤 **User**
Create a Venn diagram to visualize these overlaps

---

🧠 **Assistant**

The Venn diagram shows the overlap between the two gene sets for **OSD-48 Space Flight vs Ground Control (Carcass)**:

- 🔵 **38 genes** hypermethylated in promoters only
- 🔴 **26 genes** downregulated only
- 🟣 **9 genes** in both — the overlap genes (*Acaa1a, Cux2, Fgfr2, Galk1, Gzf1, Pcsk4, Per1, Tgm1, Tns2*) are labeled directly in the intersection

<img src="https://raw.githubusercontent.com/asaravia-butler/mcp-genelab/main/docs/examples/HBISS_03-2026_Demos/images/OSD-48_Venn.png" 
     alt="hypermethylation v downregulation Venn" 
     width="750">

---

👤 **User**
Use @monarchkg to explore pathway enrichment for these overlapping genes

---

🧠 **Assistant**

Here's the pathway enrichment map for the 9 overlap genes, drawn from Monarch KG / Reactome.

<img src="https://raw.githubusercontent.com/asaravia-butler/mcp-genelab/main/docs/examples/HBISS_03-2026_Demos/images/OSD-48_Pthwy_Enr.png" 
     alt="pathway enrichment map" 
     width="800">


### Pathway Summary

**FGFR2** — by far the most pathway-connected gene, participating in **14 Reactome pathways** spanning FGFR2 ligand binding, FRS/SHC/PI3K/MAPK/PLC downstream cascades, and disease-linked signaling.

**PER1** — maps squarely into **4 core circadian clock pathways** (BMAL:CLOCK activation → CRY:PER repression → PER degradation), reinforcing the circadian rhythm disruption theme.

**ACAA1** — involved in **peroxisomal fatty acid β-oxidation** (very long chain and ALA metabolism), neutrophil degranulation, and peroxisomal protein import.

**GALK1** — specifically in **galactose catabolism**; its disruption may impair glycan synthesis and energy metabolism.

**TGM1** — participates in **cornified envelope formation** (skin barrier/ECM crosslinking).

**CUX2, PCSK4, TNS2, GZF1** — no current Reactome annotations in Monarch.

---

👤 **User**
Search @pubmed for peer-reviewed publications that support these findings and return: PMID, Title, Year, Brief relevance summary (1 sentence)

---

🧠 **Assistant**

Based on articles retrieved from PubMed, here are the most relevant peer-reviewed publications supporting the findings from OSD-48:

## 📚 PubMed Literature Support — OSD-48 Overlap Genes

### 🧬 Spaceflight & DNA Methylation (General)

| PMID | Title | Year | Relevance |
|------|-------|------|-----------|
| **34691539** | [Development of mouse preimplantation embryos in space](https://doi.org/10.1093/nsr/nwaa062) | 2020 | Demonstrates that spaceflight induces global DNA hypomethylation and differentially methylated regions in mouse cells, directly supporting the concept of spaceflight-driven epigenetic reprogramming. |
| **31421852** | [Changes in one-carbon metabolism and DNA methylation in hearts of mice exposed to oxygen ions](https://doi.org/10.1016/j.lssr.2019.05.003) | 2019 | Shows that space-relevant radiation causes dynamic hypermethylation in the cardiac epigenome of mice, paralleling the promoter hypermethylation patterns observed in OSD-48. |
| **24585548** | [Long-term epigenetic effects of 56Fe in the mouse lung](https://doi.org/10.1093/jrr/rru010) | 2014 | Reports global DNA hypermethylation and gene-specific promoter hypermethylation (with transcriptional repression) in mice exposed to heavy ion radiation, mirroring the methylation–expression coupling observed here. |
| **31443374** | [Mice exposed to combined chronic irradiation and microgravity develop neurological sequelae](https://doi.org/10.3390/ijms20174094) | 2019 | Using RNA-seq and RRBS, demonstrates that combined microgravity+radiation alters gene expression and **promoter methylation** simultaneously in multiple pathways — a direct methodological parallel to this OSD-48 analysis. |

### 🕐 Circadian Clock Disruption (*Per1*, *Per3*, *Dbp*, *Ciart*)

| PMID | Title | Year | Relevance |
|------|-------|------|-----------|
| **41090408** | [Circadian disruption in astronauts: Causes, molecular mechanisms, and neurocognitive consequences](https://doi.org/10.1080/07420528.2025.2569695) | 2025 | Systematic review of 65 studies confirming that microgravity and spaceflight dysregulate core circadian clock genes, directly supporting the downregulation of *Per1*, *Per3*, *Dbp*, and *Ciart* in OSD-48. |
| **36203395** | [Spaceflight effects including circadian clock dysregulation: Implications of melatonin](https://doi.org/10.1111/jpi.12834) | 2022 | Reviews spaceflight-induced circadian clock disruption in astronauts, linking melatonin dysregulation and light-dark cycle alterations to downstream clock gene suppression. |
| **31179760** | [Methylation profile of circadian gene promoters in breast cancer](https://doi.org/10.1080/07420528.2019.1617732) | 2019 | Demonstrates that *PER1/PER3* promoter hypermethylation is a recurrent epigenetic silencing mechanism for clock gene suppression, directly analogous to the *Per1* promoter hypermethylation + downregulation observed in OSD-48. |
| **29524919** | [Placental circadian pathway methylation and fine particle exposure](https://doi.org/10.1016/j.envint.2018.02.034) | 2018 | Shows that environmental stressors cause promoter methylation of circadian genes (*PER1*, *PER2*, *PER3*, *CRY1*) in vivo, establishing the precedent that stress-induced CpG methylation silences the circadian program. |

### 🧪 Spaceflight & Lipid/Metabolic Dysregulation (*Acaa1a*, *Galk1*)

| PMID | Title | Year | Relevance |
|------|-------|------|-----------|
| **33242417** | [Comprehensive multi-omics reveals mitochondrial stress as a central hub for spaceflight](https://doi.org/10.1016/j.cell.2020.11.002) | 2020 | Landmark *Cell* paper using NASA GeneLab data across 59 astronauts identifying lipid metabolism, circadian rhythm, and mitochondrial pathways as the top dysregulated processes in spaceflight — directly corroborating the metabolic and circadian findings in OSD-48. |
| **27097220** | [Spaceflight activates lipotoxic pathways in mouse liver](https://doi.org/10.1371/journal.pone.0152877) | 2016 | Reports that spaceflight in mice activates PPARα-mediated lipid metabolism pathways and signs of liver injury, supporting the epigenetic downregulation of *Acaa1a* as part of broader hepatic lipid dysregulation. |
| **36070186** | [Effects of long-term simulated microgravity on liver metabolism in rhesus macaques](https://doi.org/10.1096/fj.202200544RR) | 2022 | Demonstrates that simulated microgravity in primates causes disturbed fatty acid metabolism and transcriptomic changes in the liver, extending the mouse findings of OSD-48 to a non-human primate model. |
| **36267920** | [Muscle atrophy phenotype gene expression during spaceflight linked to metabolic crosstalk in liver and muscle](https://doi.org/10.1016/j.isci.2022.105213) | 2022 | Identifies impaired lipid metabolism gene expression in mouse liver during spaceflight (NASA RR1 mission) as paired with muscle atrophy, providing direct transcriptomic context for the *Acaa1a* downregulation in OSD-48. |

### 🔵 Focal Adhesion / Mechanosensing (*Tns2*)

| PMID | Title | Year | Relevance |
|------|-------|------|-----------|
| **24903274** | [Rac1 GTPase silencing counteracts microgravity-induced effects on osteoblastic cells](https://doi.org/10.1096/fj.14-249714) | 2014 | Demonstrates that microgravity reduces focal contacts and F-actin by ~50–60% in cells exposed to real spaceflight, supporting the relevance of *Tns2* (focal adhesion protein) downregulation in OSD-48 as part of broader mechanosensing disruption. |

---

*Created by [mcp-genelab](https://github.com/sbl-sdsc/mcp-genelab) on 2026-03-11*

*Model: claude-sonnet-4-6*

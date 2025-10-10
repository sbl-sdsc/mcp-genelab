# Query-1
@genelab-remote-cypher
# Model-1
Sonnet 4.5
# Response-1
## Database Structure

**Node Types:**
- **Study** - Space biology research studies with project information
- **Mission** - Space missions (with flight programs, dates)
- **Assay** - Experimental measurements (gene expression, methylation)
- **MGene** - Mouse genes with differential expression data
- **MethylationRegion** - DNA methylation regions
- **Gene** - General gene entities
- **Anatomy** - Anatomical structures/organs
- **CellType** - Cell type classifications

**Key Relationships:**
- Studies → Assays (PERFORMED)
- Missions → Studies (CONDUCTED)
- Assays → MGenes (MEASURED_DIFFERENTIAL_EXPRESSION)
- Assays → Anatomy/CellType (INVESTIGATED)

## What can I help you with?

I can help you:
- **Find upregulated/downregulated genes** in specific organisms and tissues during spaceflight
- **Query study information** about space biology experiments
- **Explore missions** and their associated research
- **Search for specific genes** and their expression patterns
- **Investigate tissue-specific** responses to spaceflight

**Example queries:**
- "What genes are upregulated in mouse liver during spaceflight?"
- "Show me studies involving Mus musculus"
- "What missions have been conducted?"

What would you like to explore?

---

# Query-2
@genelab-local-cypher @spokeokn-cypher
# Model-2
Sonnet 4.5
# Response-2
I have access to two Neo4j knowledge graph databases through Cypher query tools:

## 1. **GeneLab Local Cypher** (genelab-local-cypher)
This appears to be a local instance of NASA GeneLab data, which likely contains:
- Space biology and genomics experimental data
- Gene expression studies from spaceflight experiments
- Relationships between genes, organisms, tissues, and experimental conditions
- Tools to find upregulated/downregulated genes in space vs. ground conditions

## 2. **SPOKE OKN Cypher** (spokeokn-cypher)
SPOKE (Scalable Precision Medicine Open Knowledge Engine) is a biomedical knowledge graph that likely contains:
- Biomedical entities and relationships
- Drug-disease-gene associations
- Protein interactions
- Clinical and biological data

Both databases support:
- **Schema exploration** - I can view the node types, relationships, and their properties
- **Cypher queries** - I can execute read queries to retrieve information
- **Metadata access** - I can get descriptions of nodes and relationships
- **Specialized tools** - The GeneLab database has specific tools for finding differentially expressed genes

Would you like me to:
1. Show you the schema of either database?
2. Query specific information from these knowledge graphs?
3. Find upregulated or downregulated genes for a particular organism and tissue?
4. Explore what kinds of biomedical relationships are available?

Let me know what you'd like to investigate!

---

# Query-3
Describe the Assay node and its properties in @genelab-remote-cypher, and include an example for a ground control vs space flight comparison.
# Model-3
Sonnet 4.5
# Response-3
The **Assay** node represents a biological measurement or experiment that compares two experimental conditions, typically Ground Control versus Space Flight. It captures the complete experimental design including the biological materials tested, the measurement technology used, and the experimental factors being compared.

### Properties

**Core Identification:**
- **identifier**: Unique assay identifier combining GeneLab dataset accession and MD5 hash of metadata (ensures version integrity)
- **name**: Human-readable assay title detailing the biological measurement performed

**Experimental Factors (Comparison Groups):**
- **factor_space_1**: Primary experimental grouping (e.g., "Ground Control", "Basal Control", "Vivarium Control")
- **factor_space_2**: Secondary experimental grouping for comparison (e.g., "Space Flight")
- **factors_1**: Array of primary experimental factors applied (independent variables)
- **factors_2**: Array of secondary experimental factors for comparison

**Biological Materials:**
- **material_1**: Biological material in the first assay group (e.g., tissue, cell type, organ)
- **material_2**: Biological material in the second assay group
- **material_name_1**: Preferred ontology-based name for first material (e.g., UBERON term)
- **material_name_2**: Preferred ontology-based name for second material
- **material_id_1**: Controlled ontology identifier (e.g., UBERON code)
- **material_id_2**: Controlled ontology identifier for second material

**Measurement Details:**
- **technology**: Platform or method used (e.g., "RNA Sequencing (RNA-Seq)")
- **measurement**: Type of data produced (e.g., "transcription profiling")

### Relationships

- **MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG** → MGene (genes with differential expression)
- **MEASURED_DIFFERENTIAL_METHYLATION_ASmMR** → MethylationRegion (differentially methylated regions)
- **INVESTIGATED_ASiCT** → CellType (cell types investigated)
- **INVESTIGATED_ASiA** → Anatomy (anatomical structures investigated)
- **PERFORMED_SpAS** ← Study (study that performed this assay)

### Example: Ground Control vs Space Flight Comparison

Here's a real example from study OSD-100 examining the effect of spaceflight on mouse eye tissue:

```
{
  "identifier": "OSD-100-f78ed5850e6775b0cc9cf976509971d4",
  "name": "OSD-100_transcription-profiling_rna-sequencing-(rna-seq)",
  
  "factor_space_1": "Ground Control",
  "factor_space_2": "Space Flight",
  "factors_1": ["Ground Control"],
  "factors_2": ["Space Flight"],
  
  "material_1": "left eye",
  "material_2": "left eye",
  "material_name_1": "left eye",
  "material_name_2": "left eye",
  "material_id_1": "UBERON:0004548",
  "material_id_2": "UBERON:0004548",
  
  "technology": "RNA Sequencing (RNA-Seq)",
  "measurement": "transcription profiling"
}
```

**Study Context:**
- **Study**: OSD-100
- **Organism**: *Mus musculus* (mouse)
- **Comparison**: This assay compares gene expression in left eye tissue between mice kept under ground control conditions versus those exposed to spaceflight conditions

This assay would have connections to MGene nodes representing genes that showed differential expression between the ground control and spaceflight conditions, allowing researchers to identify which genes are affected by spaceflight in eye tissue.

---

# Query-4
Describe the MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG relationship and its properties in @genelab-remote-cypher, and include an example for a ground control vs. space flight comparison.
# Model-4
Sonnet 4.5
# Response-4
## MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG Relationship

### Overview
The **MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG** relationship connects **Assay** nodes to **MGene** (Model Organism Gene) nodes in the genelab-remote-cypher knowledge graph. This relationship captures differential gene expression data from experiments comparing different conditions, such as space flight versus ground control.

### Relationship Properties

The relationship has two key properties:

1. **log2fc** (float)
   - **Description**: Log2-transformed fold change in gene expression between the experimental condition and its reference baseline
   - **Interpretation**: 
     - **Positive values** indicate **upregulation** (higher transcript abundance in the experimental condition)
     - **Negative values** indicate **downregulation** (lower transcript abundance in the experimental condition)
   - **Example**: A log2fc of 50.36 means the gene is ~2^50 times more expressed in the experimental condition

2. **adj_p_value** (float)
   - **Description**: Multiple testing-corrected p-value (FDR-adjusted) assessing the statistical significance of differential expression
   - **Interpretation**: 
     - Smaller values indicate higher confidence that the observed change is not due to chance
     - Common threshold: **adj_p_value ≤ 0.05** indicates statistical significance
     - Controls for false discoveries when testing thousands of genes simultaneously

### Ground Control vs. Space Flight Example

Here's a real example from the knowledge graph:

#### Upregulated Gene Example:
- **Study**: OSD-516
- **Assay**: RNA-Sequencing (Illumina)
- **Material**: Primary cell
- **Organism**: *Homo sapiens* (human)
- **Gene Symbol**: LOC102724560
- **Gene Name**: Cystathionine beta-synthase like
- **log2fc**: +50.36
- **adj_p_value**: 3.64e-49
- **Interpretation**: This gene shows massive upregulation in space flight conditions compared to ground control (extremely significant, p < 0.001)

#### Downregulated Gene Example:
- **Study**: OSD-253
- **Assay**: RNA-Sequencing (Illumina NovaSeq 6000)
- **Material**: Left kidney
- **Organism**: *Mus musculus* (mouse)
- **Gene Symbol**: Gvin-ps4
- **Gene Name**: GTPase, very large interferon inducible, pseudogene 4
- **log2fc**: -48.25
- **adj_p_value**: 1.23e-141
- **Interpretation**: This gene shows dramatic downregulation in space flight conditions compared to ground control (extremely significant)

### Usage Pattern

This relationship enables researchers to identify which genes are significantly affected by space flight conditions across different organisms, tissues, and experimental designs, providing insights into molecular responses to the space environment.

---

# Query-5
Give a breakdown of missions, studies, and the type of technologies used in the studies in @genelab-remote-cypher
# Model-5
Sonnet 4.5
# Response-5
## Overview
The GeneLab knowledge graph contains **140 studies** across **23 space missions** (ranging from 1991-2021), comprising **6,467 total assays** that investigate the effects of spaceflight and related conditions on biological systems.

---

## **Mission Breakdown**

### **Key Missions by Program:**

**Space Shuttle Era (1991-2011)**
- **STS-40** (1991): 1 study, 4 assays
- **STS-58** (1993): 1 study, 2 assays  
- **STS-131** (2010): 1 study, 12 assays
- **STS-135** (2011): 2 studies, 12 assays

**International Space Station Era (2006-present)**
- **SpaceX Resupply Missions** (SpaceX-4 through SpaceX-21): The most active period
  - **SpaceX-4** (2014): 13 studies, 173 assays
  - **SpaceX-12** (2017): 8 studies, 218 assays
  - **SpaceX-13** (2017): 6 studies, 240 assays
  - **SpaceX-15** (2018): 2 studies, 546 assays
  - **SpaceX-16** (2018): 2 studies, 510 assays
  - **SpaceX-21** (2020): 17 studies, 428 assays

**International Partnerships:**
- **Soyuz missions** (ESA, Italian Space Agency)
- **Northrop Grumman** (NG-11)
- Collaborations with **JAXA** (Japanese space agency)

---

## **Study Types**

### **By Project Type:**
- **Spaceflight Studies**: 82 studies (primary focus)
- **Ground Studies**: 53 studies (control experiments)
- **High Altitude Studies**: 4 studies
- **Ground study** (alternate naming): 2 studies

### **By Organism:**
- **Mus musculus (mice)**: 99 studies
  - 69 spaceflight studies
  - 29 ground studies
  - 1 spaceflight study (alternate naming)
  
- **Homo sapiens (humans)**: 36 studies
  - 10 spaceflight studies
  - 21 ground studies
  - 4 high altitude studies
  - 1 ground study (alternate naming)
  
- **Rattus norvegicus (rats)**: 5 studies
  - 2 spaceflight studies
  - 2 ground studies
  - 1 ground study (alternate naming)

---

## **Technologies & Measurements**

### **Primary Technologies Used:**

1. **RNA Sequencing (RNA-Seq)**: 4,512 assays (70% of total)
   - Primary modern technology for gene expression analysis
   - Used across most recent missions (SpaceX era)
   - Measurement type: Transcription profiling

2. **DNA Microarray**: 1,944 assays (30% of total)
   - Older technology, primarily in earlier missions
   - Measurement type: Transcription profiling

3. **Whole Genome Bisulfite Sequencing**: 11 assays (<1%)
   - Specialized technique for epigenetics
   - Measurement type: DNA methylation profiling

### **Measurement Categories:**
- **Transcription profiling**: 6,456 assays (gene expression)
- **DNA methylation profiling**: 11 assays (epigenetic modifications)

---

## **Notable Study Series**

### **Rodent Research (RR) Program:**
The most extensive research series focused on mice:
- **RR-1** (Rodent Research 1): Multiple studies (OSD-100 through OSD-105)
- **RR-3** (Rodent Research 3): Studies across SpaceX-8, 9, 10, 11 missions
- **RR-5** (Rodent Research 5): 2 studies on SpaceX-15
- **RR-6** (Rodent Research 6): 6 studies on SpaceX-16
- **RR-9** (Rodent Research 9): 1 study on SpaceX-16

### **Mouse Habitat Unit Studies:**
- MHU-2 experiments on SpaceX missions

### **Commercial Biomedical Test Modules:**
- CBTM-3 on SpaceX-9

---

## **Research Focus Areas**

Based on study titles and investigations, the research encompasses:

- **Cardiovascular health**: Space radiation effects on myocardium
- **Immune system**: T-cell activation in microgravity
- **Musculoskeletal system**: Effects of microgravity on bone and muscle
- **Gene expression**: Transcriptional changes across multiple tissue types
- **Epigenetics**: DNA methylation patterns in space conditions
- **Radiation biology**: Combined effects of radiation and microgravity

---

## **Data Scale**

- **Total Missions**: 23
- **Total Studies**: 140
- **Total Assays**: 6,467
- **Space Agencies Involved**: NASA, ESA, JAXA, Italian Space Agency (ASI)
- **Mission Duration Range**: 1991-2021 (30 years of research)

The knowledge graph represents a comprehensive archive of space biology research, with a clear evolution from microarray-based approaches in early missions to RNA-Seq dominated modern studies, primarily investigating rodent models with complementary human studies.

---

# Query-6
Find the top 5 upregulated genes in mouse liver tissue during Space Flight in @genelab-remote-cypher.
# Model-6
Sonnet 4.5
# Response-6
Here are the **top 5 upregulated genes in mouse liver tissue during space flight** from study OSD-48:

1. **Rptn (Repetin)** - Log2 fold change: 32.86, adjusted p-value: 1.06×10⁻⁵
   
2. **Krt5 (Keratin 5)** - Log2 fold change: 32.54, adjusted p-value: 1.21×10⁻⁵

3. **Sprr3 (Small proline-rich protein 3)** - Log2 fold change: 27.37, adjusted p-value: 0.001

4. **Krt13 (Keratin 13)** - Log2 fold change: 27.09, adjusted p-value: 0.001

5. **Krtdap (Keratinocyte differentiation-associated protein)** - Log2 fold change: 25.25, adjusted p-value: 0.005

**Key observations:**

These genes show dramatic upregulation during space flight compared to ground controls, with log2 fold changes ranging from 25 to 33 (representing millions-fold increases in expression). Interestingly, all five genes are related to **keratinocyte differentiation and epithelial structure**:

- **Keratins (Krt5, Krt13)** are structural proteins in epithelial cells
- **Rptn** is involved in cornification and epithelial differentiation
- **Sprr3** is part of the cornified envelope in epithelial tissues
- **Krtdap** supports keratinocyte differentiation

This suggests that space flight may induce significant changes in epithelial cell biology or tissue remodeling in mouse liver, which is an unexpected finding given these genes are typically associated with skin and stratified epithelial tissues rather than liver.

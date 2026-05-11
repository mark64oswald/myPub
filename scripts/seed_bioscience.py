#!/usr/bin/env python3
"""
seed_bioscience.py — Seed bioscience + bioinformatics doc_sources (2026-05-10).

Standalone bioscience expansion. Lives in its own file (rather than appending
to ``seed_doc_sources.py``) because that file has uncommitted edits in a
parallel session — keeping seeds split avoids a merge conflict.

Tier ordering follows the bioscience-software stack:
  T1 Foundational    — file-format parsers (FASTA/BAM/VCF...)
  T2 Core analysis   — aligners + variant callers + structure prediction
  T3 Pipelines       — Nextflow / Snakemake / WDL / Galaxy
  T4 Cohort / omics  — Hail, Glow, scverse, Bioconductor SE
  T5 Clinical interp — cBioPortal, OncoKB, CIViC, PharmCAT, VEP, Phenopackets
  Bridge             — FHIR Genomics IG + VICC g2p-aggregator (genomics ↔ FHIR)

Default to DeepWiki (per Rust+PDF expansion finding — Context7 ships ~20-section
stubs for OSS libs; DeepWiki ships 300-800-section architectural docs).

Idempotent: reuses ``seed_doc_sources._upsert`` so re-runs UPDATE name/authority/
ttl but preserve runtime state.

Usage:
    .venv/bin/python3 scripts/seed_bioscience.py
    .venv/bin/python3 scripts/seed_bioscience.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import duckdb

# Reuse the upsert + validation from the canonical seed module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_doc_sources import _upsert, DEFAULT_AUTHORITY  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("seed_bioscience")


BIOSCIENCE_SEEDS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------
    # T1 — Foundational: file-format parsers & sequence I/O.
    # ------------------------------------------------------------------
    {"name": "Biopython",        "source_type": "deepwiki", "identifier": "biopython/biopython",         "refresh_ttl_days": 30},
    {"name": "scikit-bio",       "source_type": "deepwiki", "identifier": "scikit-bio/scikit-bio",       "refresh_ttl_days": 60},
    {"name": "pysam",            "source_type": "deepwiki", "identifier": "pysam-developers/pysam",     "refresh_ttl_days": 30},
    {"name": "BioJava",          "source_type": "deepwiki", "identifier": "biojava/biojava",             "refresh_ttl_days": 60},
    {"name": "BioPerl",          "source_type": "deepwiki", "identifier": "bioperl/bioperl-live",        "refresh_ttl_days": 90},

    # ------------------------------------------------------------------
    # T2 — Core analysis: aligners, variant callers, structure prediction.
    # Skipping BLAST+ (Context7 "blast" lexical false-match risk; revisit
    # when we hand-pick the right repo path).
    # ------------------------------------------------------------------
    {"name": "GATK",             "source_type": "deepwiki", "identifier": "broadinstitute/gatk",         "refresh_ttl_days": 30},
    {"name": "DeepVariant",      "source_type": "deepwiki", "identifier": "google/deepvariant",          "refresh_ttl_days": 30},
    {"name": "BWA",              "source_type": "deepwiki", "identifier": "lh3/bwa",                     "refresh_ttl_days": 90},
    {"name": "samtools",         "source_type": "deepwiki", "identifier": "samtools/samtools",           "refresh_ttl_days": 30},
    {"name": "htslib",           "source_type": "deepwiki", "identifier": "samtools/htslib",             "refresh_ttl_days": 30},
    {"name": "bcftools",         "source_type": "deepwiki", "identifier": "samtools/bcftools",           "refresh_ttl_days": 30},
    {"name": "AlphaFold",        "source_type": "deepwiki", "identifier": "google-deepmind/alphafold",   "refresh_ttl_days": 30},
    {"name": "OpenFold",         "source_type": "deepwiki", "identifier": "aqlaboratory/openfold",       "refresh_ttl_days": 30},
    {"name": "Bowtie2",          "source_type": "deepwiki", "identifier": "BenLangmead/bowtie2",         "refresh_ttl_days": 90},
    {"name": "HISAT2",           "source_type": "deepwiki", "identifier": "DaehwanKimLab/hisat2",        "refresh_ttl_days": 90},

    # ------------------------------------------------------------------
    # T3 — Pipelines: workflow engines + reference pipelines.
    # ------------------------------------------------------------------
    {"name": "Nextflow",         "source_type": "deepwiki", "identifier": "nextflow-io/nextflow",        "refresh_ttl_days": 14},
    {"name": "nf-core/sarek",    "source_type": "deepwiki", "identifier": "nf-core/sarek",               "refresh_ttl_days": 30},
    {"name": "nf-core/rnaseq",   "source_type": "deepwiki", "identifier": "nf-core/rnaseq",              "refresh_ttl_days": 30},
    {"name": "Snakemake",        "source_type": "deepwiki", "identifier": "snakemake/snakemake",         "refresh_ttl_days": 30},
    {"name": "Cromwell",         "source_type": "deepwiki", "identifier": "broadinstitute/cromwell",     "refresh_ttl_days": 30},
    {"name": "WDL",              "source_type": "deepwiki", "identifier": "openwdl/wdl",                 "refresh_ttl_days": 60},
    {"name": "Galaxy",           "source_type": "deepwiki", "identifier": "galaxyproject/galaxy",        "refresh_ttl_days": 30},

    # ------------------------------------------------------------------
    # T4 — Cohort + multi-omics. The Databricks-relevant tier.
    # ------------------------------------------------------------------
    {"name": "Hail",             "source_type": "deepwiki", "identifier": "hail-is/hail",                "refresh_ttl_days": 14},
    {"name": "Glow",             "source_type": "deepwiki", "identifier": "projectglow/glow",            "refresh_ttl_days": 30},
    {"name": "ADAM",             "source_type": "deepwiki", "identifier": "bigdatagenomics/adam",        "refresh_ttl_days": 60},
    {"name": "Scanpy",           "source_type": "deepwiki", "identifier": "scverse/scanpy",              "refresh_ttl_days": 14},
    {"name": "anndata",          "source_type": "deepwiki", "identifier": "scverse/anndata",             "refresh_ttl_days": 30},
    {"name": "Seurat",           "source_type": "deepwiki", "identifier": "satijalab/seurat",            "refresh_ttl_days": 30},
    {"name": "MultiQC",          "source_type": "deepwiki", "identifier": "MultiQC/MultiQC",             "refresh_ttl_days": 30},
    {"name": "SummarizedExperiment", "source_type": "deepwiki", "identifier": "Bioconductor/SummarizedExperiment", "refresh_ttl_days": 60},

    # ------------------------------------------------------------------
    # T5 — Clinical interpretation: cancer + pharmacogenomics + general.
    # ------------------------------------------------------------------
    {"name": "cBioPortal",       "source_type": "deepwiki", "identifier": "cBioPortal/cbioportal",       "refresh_ttl_days": 30},
    {"name": "cBioPortalData",   "source_type": "deepwiki", "identifier": "waldronlab/cBioPortalData",   "refresh_ttl_days": 60},
    {"name": "cbioportal-mcp",   "source_type": "deepwiki", "identifier": "cBioPortal/cbioportal-mcp",   "refresh_ttl_days": 30},
    {"name": "OncoKB annotator", "source_type": "deepwiki", "identifier": "oncokb/oncokb-annotator",     "refresh_ttl_days": 30},
    {"name": "CIViC",            "source_type": "deepwiki", "identifier": "griffithlab/civic-server",    "refresh_ttl_days": 60},
    {"name": "PharmCAT",         "source_type": "deepwiki", "identifier": "PharmGKB/PharmCAT",           "refresh_ttl_days": 30},
    {"name": "PyPGx",            "source_type": "deepwiki", "identifier": "sbslee/pypgx",                "refresh_ttl_days": 60},
    {"name": "VEP",              "source_type": "deepwiki", "identifier": "Ensembl/ensembl-vep",         "refresh_ttl_days": 30},
    {"name": "Phenopackets",     "source_type": "deepwiki", "identifier": "phenopackets/phenopacket-schema", "refresh_ttl_days": 60},

    # ------------------------------------------------------------------
    # Bridge — genomics ↔ FHIR + cross-source variant interpretation.
    # ------------------------------------------------------------------
    {"name": "FHIR Genomics Reporting IG", "source_type": "deepwiki", "identifier": "HL7/genomics-reporting", "refresh_ttl_days": 60},
    {"name": "VICC g2p-aggregator",        "source_type": "deepwiki", "identifier": "cancervariants/g2p-aggregator", "refresh_ttl_days": 60},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.dry_run:
        for seed in BIOSCIENCE_SEEDS:
            authority = seed.get("authority_score",
                                 DEFAULT_AUTHORITY[seed["source_type"]])
            LOG.info("would seed %-10s %-40s  auth=%.2f  ttl=%dd",
                     seed["source_type"], seed["identifier"],
                     authority, seed["refresh_ttl_days"])
        LOG.info("dry-run: %d seeds", len(BIOSCIENCE_SEEDS))
        return 0

    conn = duckdb.connect(str(args.catalog))
    try:
        conn.execute("BEGIN")
        counts = {"inserted": 0, "updated": 0}
        for seed in BIOSCIENCE_SEEDS:
            action = _upsert(conn, seed)
            counts[action] += 1
            LOG.info("%s %-10s %s", action.upper(), seed["source_type"], seed["identifier"])
        conn.execute("COMMIT")
        LOG.info("done: %d inserted, %d updated (%d total)",
                 counts["inserted"], counts["updated"],
                 counts["inserted"] + counts["updated"])
        total = conn.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0]
        LOG.info("doc_source row count: %d", total)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

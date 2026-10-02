"""Ingestion: normalization, deduplication, and persistence of source events."""

from sloperator.ingest.pipeline import IngestPipeline, IngestStats, normalize

__all__ = ["IngestPipeline", "IngestStats", "normalize"]

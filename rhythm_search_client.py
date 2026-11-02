"""Backward-compatible import path for the renamed Search Engine client."""

from search_engine_client import SearchEngineClient

RhythmSearchClient = SearchEngineClient

__all__ = ["RhythmSearchClient", "SearchEngineClient"]

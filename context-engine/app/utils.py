"""
utils.py — shared helper functions for context-engine.
"""

from . import config

def chunk_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split text into overlapping chunks of a given character length."""
    size = chunk_size if chunk_size is not None else config.DEFAULT_CHUNK_SIZE
    lap  = overlap if overlap is not None else config.DEFAULT_CHUNK_OVERLAP
    
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:min(start + size, len(text))])
        start += size - lap
    return chunks

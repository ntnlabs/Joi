"""
Auto-ingestion module for Joi RAG knowledge.

Watches /var/lib/joi/ingestion/input/<scope>/ for new files and
automatically ingests them into the knowledge base.

Environment variables:
    JOI_INGESTION_DIR: Base ingestion directory (default: /var/lib/joi/ingestion)
    JOI_INGESTION_KEEP_FILES: Keep original files (default: 0)
        0 = touch marker in done/, delete original (saves space)
        1 = move original to done/ (keeps content)
    JOI_INGESTION_CHUNK_SIZE: Chunk size in characters (default: 500)
    JOI_INGESTION_OVERLAP: Overlap between chunks (default: 50)
"""

import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from memory import MemoryStore

logger = logging.getLogger("joi.ingestion")

# Configuration
INGESTION_DIR = Path(os.getenv("JOI_INGESTION_DIR", "/var/lib/joi/ingestion"))
KEEP_FILES = os.getenv("JOI_INGESTION_KEEP_FILES", "0") == "1"
CHUNK_SIZE = int(os.getenv("JOI_INGESTION_CHUNK_SIZE", "500"))
OVERLAP = int(os.getenv("JOI_INGESTION_OVERLAP", "50"))

# Supported file extensions
SUPPORTED_EXTENSIONS = {".txt", ".md"}


def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[str]:
    """
    Split text into overlapping chunks.

    Args:
        text: Text to split
        chunk_size: Target size per chunk (in characters)
        overlap: Overlap between chunks

    Returns:
        List of text chunks
    """
    if len(text) <= chunk_size:
        return [text.strip()] if text.strip() else []

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        # Try to break at paragraph or sentence boundary
        if end < len(text):
            # Look for paragraph break
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + chunk_size // 2:
                end = para_break + 2
            else:
                # Look for sentence break
                for sep in [". ", ".\n", "! ", "? "]:
                    sent_break = text.rfind(sep, start, end)
                    if sent_break > start + chunk_size // 2:
                        end = sent_break + len(sep)
                        break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Move start with overlap
        start = end - overlap if end < len(text) else len(text)

    return chunks


def parse_original_filename(stored_name: str) -> str:
    """
    Extract original filename from stored name.

    Stored format: {timestamp}_{original_filename}
    Example: 1708444800_notes.txt -> notes.txt
    """
    if "_" in stored_name:
        prefix = stored_name.split("_")[0]
        if prefix.isdigit():
            return "_".join(stored_name.split("_")[1:])
    return stored_name


def extract_title(text: str, source: str) -> str:
    """Extract title from document text or use filename."""
    lines = text.strip().split("\n")

    # Check for markdown title
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip()

    # Check for first non-empty line as title
    for line in lines[:5]:
        line = line.strip()
        if line and len(line) < 100:
            return line

    # Fall back to filename (use original name, not stored name with timestamp)
    original_name = parse_original_filename(Path(source).name)
    return Path(original_name).stem.replace("-", " ").replace("_", " ").title()


def ingest_file(
    filepath: Path,
    memory: MemoryStore,
    scope: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = OVERLAP,
) -> int:
    """
    Ingest a single file into the knowledge base.

    Args:
        filepath: Path to file
        memory: MemoryStore instance
        scope: Access scope (conversation_id)
        chunk_size: Characters per chunk
        overlap: Overlap between chunks

    Returns:
        Number of chunks created
    """
    # Read file
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read %s: %s", filepath, e)
        return 0

    if not text.strip():
        logger.warning("Skipping empty file: %s", filepath)
        return 0

    # Source identifier uses scope/filename for uniqueness
    source = f"{scope}/{filepath.name}"

    # Extract title
    title = extract_title(text, filepath.name)

    # Delete existing chunks for this source (re-ingest)
    memory.delete_knowledge_source(source)

    # Split into chunks
    chunks = chunk_text(text, chunk_size, overlap)

    if not chunks:
        logger.warning("No chunks generated from: %s", filepath)
        return 0

    # Store chunks
    for i, chunk_content in enumerate(chunks):
        memory.store_knowledge_chunk(
            source=source,
            title=title,
            content=chunk_content,
            chunk_index=i,
            scope=scope,
        )

    logger.info("Ingested %s: %d chunks (scope: %s)", filepath.name, len(chunks), scope)
    return len(chunks)


def ensure_directories() -> Tuple[Path, Path]:
    """Ensure ingestion directories exist. Returns (input_dir, done_dir)."""
    input_dir = INGESTION_DIR / "input"
    done_dir = INGESTION_DIR / "done"

    input_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)

    return input_dir, done_dir


def mark_done(filepath: Path, scope: str, done_dir: Path) -> None:
    """
    Mark a file as processed.

    If KEEP_FILES=1: move to done/<scope>/
    If KEEP_FILES=0: touch marker in done/<scope>/, delete original
    """
    scope_done_dir = done_dir / scope
    scope_done_dir.mkdir(parents=True, exist_ok=True)

    dest = scope_done_dir / filepath.name

    if KEEP_FILES:
        # Move original to done/
        shutil.move(str(filepath), str(dest))
        logger.debug("Moved %s to %s", filepath, dest)
    else:
        # Touch marker, delete original
        dest.touch()
        filepath.unlink()
        logger.debug("Marked %s as done, deleted original", filepath.name)


def process_pending(memory: MemoryStore) -> Tuple[int, int]:
    """
    Process all pending files in ingestion input directories.

    Returns:
        (files_processed, total_chunks)
    """
    input_dir, done_dir = ensure_directories()

    files_processed = 0
    total_chunks = 0

    # Scan for scope directories
    if not input_dir.exists():
        return 0, 0

    for scope_dir in input_dir.iterdir():
        if not scope_dir.is_dir():
            continue

        scope = scope_dir.name

        # Process files in this scope directory
        for filepath in scope_dir.iterdir():
            if not filepath.is_file():
                continue

            # Skip hidden/temp files (e.g., .filename.tmp from atomic writes)
            if filepath.name.startswith("."):
                continue

            if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                logger.debug("Skipping unsupported file: %s", filepath)
                continue

            # Check if already processed
            marker = done_dir / scope / filepath.name
            if marker.exists():
                logger.debug("Skipping already processed: %s", filepath)
                continue

            # Ingest
            try:
                chunks = ingest_file(filepath, memory, scope)
                if chunks > 0:
                    mark_done(filepath, scope, done_dir)
                    files_processed += 1
                    total_chunks += chunks
            except Exception as e:
                logger.error("Failed to ingest %s: %s", filepath, e)

    return files_processed, total_chunks


def run_auto_ingestion(memory: MemoryStore) -> None:
    """
    Run auto-ingestion check. Called by scheduler.

    Logs summary only if files were processed.
    """
    try:
        files, chunks = process_pending(memory)
        if files > 0:
            logger.info("Auto-ingestion: processed %d files, %d chunks", files, chunks)
    except Exception as e:
        logger.error("Auto-ingestion error: %s", e)

#!/usr/bin/env python3
"""
Knowledge ingestion script for Joi RAG.

Usage:
    ./ingest-knowledge.py <file_or_directory> [--scope SCOPE] [--chunk-size 500] [--overlap 50]

Examples:
    # Ingest for a specific user
    ./ingest-knowledge.py notes.txt --scope +1234567890

    # Ingest for a group
    ./ingest-knowledge.py docs/ --scope "GroupID123"

NOTE: --scope is required! Knowledge without scope is orphaned and inaccessible.

Supports:
    - .txt files (plain text)
    - .md files (markdown)
    - Directories (recursive)
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Tuple

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
logger = logging.getLogger("ingest")


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
            para_break = text.rfind('\n\n', start, end)
            if para_break > start + chunk_size // 2:
                end = para_break + 2
            else:
                # Look for sentence break
                for sep in ['. ', '.\n', '! ', '? ']:
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


def extract_title(text: str, source: str) -> str:
    """Extract title from document text or use filename."""
    lines = text.strip().split('\n')

    # Check for markdown title
    if lines and lines[0].startswith('# '):
        return lines[0][2:].strip()

    # Check for first non-empty line as title
    for line in lines[:5]:
        line = line.strip()
        if line and len(line) < 100:
            return line

    # Fall back to filename
    return Path(source).stem.replace('-', ' ').replace('_', ' ').title()


def ingest_file(
    filepath: Path,
    memory: MemoryStore,
    chunk_size: int,
    overlap: int,
    base_path: Path,
    scope: str = "",
) -> int:
    """
    Ingest a single file.

    Args:
        filepath: Path to file
        memory: MemoryStore instance
        chunk_size: Characters per chunk
        overlap: Overlap between chunks
        base_path: Base path for relative source naming
        scope: Access scope (conversation_id or empty for global)

    Returns:
        Number of chunks created
    """
    # Read file
    try:
        text = filepath.read_text(encoding='utf-8')
    except Exception as e:
        logger.error("Failed to read %s: %s", filepath, e)
        return 0

    if not text.strip():
        logger.warning("Skipping empty file: %s", filepath)
        return 0

    # Create source identifier (relative path)
    try:
        source = str(filepath.relative_to(base_path))
    except ValueError:
        source = filepath.name

    # Extract title
    title = extract_title(text, source)

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

    scope_info = f" (scope: {scope})" if scope else " (global)"
    logger.info("Ingested %s: %d chunks%s", source, len(chunks), scope_info)
    return len(chunks)


def ingest_directory(
    directory: Path,
    memory: MemoryStore,
    chunk_size: int,
    overlap: int,
    scope: str = "",
) -> Tuple[int, int]:
    """
    Ingest all supported files in directory.

    Returns:
        (files_processed, total_chunks)
    """
    supported_extensions = {'.txt', '.md'}
    files_processed = 0
    total_chunks = 0

    for filepath in directory.rglob('*'):
        if filepath.suffix.lower() in supported_extensions:
            chunks = ingest_file(filepath, memory, chunk_size, overlap, directory, scope)
            if chunks > 0:
                files_processed += 1
                total_chunks += chunks

    return files_processed, total_chunks


def main():
    parser = argparse.ArgumentParser(description="Ingest knowledge documents for Joi RAG")
    parser.add_argument("path", help="File or directory to ingest")
    parser.add_argument("--scope", default="", help="Access scope (conversation_id). Empty = global/legacy")
    parser.add_argument("--chunk-size", type=int, default=500, help="Chunk size in characters (default: 500)")
    parser.add_argument("--overlap", type=int, default=50, help="Overlap between chunks (default: 50)")
    parser.add_argument("--db", default=None, help="Database path (default: from JOI_MEMORY_DB)")
    parser.add_argument("--list", action="store_true", help="List current knowledge sources")
    parser.add_argument("--delete", metavar="SOURCE", help="Delete a knowledge source")
    args = parser.parse_args()

    # Initialize memory store
    db_path = args.db or os.getenv("JOI_MEMORY_DB", "/var/lib/joi/memory.db")
    memory = MemoryStore(db_path)

    # Handle list command
    if args.list:
        sources = memory.get_knowledge_sources()
        if not sources:
            print("No knowledge sources found.")
        else:
            print(f"{'Source':<50} {'Chunks':>8}")
            print("-" * 60)
            for s in sources:
                print(f"{s['source']:<50} {s['chunk_count']:>8}")
        return

    # Handle delete command
    if args.delete:
        deleted = memory.delete_knowledge_source(args.delete)
        print(f"Deleted {deleted} chunks from '{args.delete}'")
        return

    # Ingest
    path = Path(args.path)

    if not path.exists():
        logger.error("Path does not exist: %s", path)
        sys.exit(1)

    # Warn if no scope - knowledge will be orphaned
    if not args.scope:
        logger.warning("No --scope provided! Knowledge will be orphaned and inaccessible.")
        logger.warning("Use --scope <conversation_id> to make knowledge searchable.")
        response = input("Continue anyway? [y/N] ").strip().lower()
        if response != 'y':
            print("Aborted.")
            sys.exit(0)

    scope_info = f" into scope '{args.scope}'" if args.scope else " (orphaned - no scope!)"

    if path.is_file():
        chunks = ingest_file(path, memory, args.chunk_size, args.overlap, path.parent, args.scope)
        print(f"Ingested 1 file, {chunks} chunks{scope_info}")
    elif path.is_dir():
        files, chunks = ingest_directory(path, memory, args.chunk_size, args.overlap, args.scope)
        print(f"Ingested {files} files, {chunks} chunks{scope_info}")
    else:
        logger.error("Path is not a file or directory: %s", path)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Note manager - per-conversation user-created notes.

Notes are named, longer-form text entries. They are searchable by title and
content via FTS5 + semantic embedding. They may carry a one-time soft reminder
date (remind_at), fired by the scheduler. No Wind coupling, no engagement tracking.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("joi.notes")


@dataclass
class Note:
    """A user note entry."""

    id: int
    conversation_id: str
    title: str
    content: str
    embedding: Optional[bytes]
    remind_at: Optional[str]      # ISO8601 UTC string or None
    created_at: int               # ms epoch
    updated_at: int               # ms epoch
    archived: bool


def _row_to_note(row: dict) -> Note:
    """Convert a MemoryStore row dict to a Note dataclass."""
    return Note(
        id=row["id"],
        conversation_id=row["conversation_id"],
        title=row["title"],
        content=row["content"],
        embedding=row.get("embedding"),
        remind_at=row.get("remind_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived=bool(row.get("archived", 0)),
    )


class NoteManager:
    """
    Manages user notes.

    Wraps MemoryStore note methods. Separate from Wind, reminders, and facts.
    All operations are per-conversation via conversation_id.
    """

    def __init__(self, memory_store):
        """
        Initialize NoteManager.

        Args:
            memory_store: A MemoryStore instance
        """
        self._store = memory_store

    def add(
        self,
        conversation_id: str,
        title: str,
        content: str,
        remind_at: Optional[str] = None,
    ) -> int:
        """
        Add a new note.

        Args:
            conversation_id: Target conversation
            title: Note name (user-supplied)
            content: Note body text
            remind_at: Optional ISO8601 UTC string for one-time soft reminder

        Returns:
            Note ID
        """
        return self._store.add_note(conversation_id, title, content, remind_at)

    def get_by_title(self, conversation_id: str, title: str) -> Optional[Note]:
        """Find active note by title (fuzzy LIKE match). Returns None if not found."""
        row = self._store.get_note_by_title(conversation_id, title)
        return _row_to_note(row) if row else None

    def append(self, note_id: int, text: str) -> None:
        """Append text to existing note content."""
        self._store.append_note_content(note_id, text)

    def replace(self, note_id: int, new_content: str) -> None:
        """Replace note content entirely."""
        self._store.replace_note_content(note_id, new_content)

    def archive(self, note_id: int) -> None:
        """Soft-delete a note."""
        self._store.archive_note(note_id)

    def list_active(self, conversation_id: str) -> List[Note]:
        """List all active notes for a conversation, newest first."""
        return [_row_to_note(r) for r in self._store.list_notes(conversation_id)]

    def search(self, conversation_id: str, query: str, limit: int = 5) -> List[Note]:
        """Search notes by FTS5 + semantic similarity."""
        return [_row_to_note(r) for r in self._store.search_notes(conversation_id, query, limit)]

    def set_remind_at(self, note_id: int, remind_at: Optional[str]) -> None:
        """Set or clear the remind_at timestamp."""
        self._store.set_note_remind_at(note_id, remind_at)

    def get_due_reminders(self) -> List[Note]:
        """Return notes with a past remind_at (for scheduler)."""
        return [_row_to_note(r) for r in self._store.get_due_note_reminders()]

    def clear_remind_at(self, note_id: int) -> None:
        """Clear remind_at after it fires."""
        self._store.clear_note_remind_at(note_id)

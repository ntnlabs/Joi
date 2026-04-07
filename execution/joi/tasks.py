"""
Task manager - per-conversation named checkable lists.

Tasks are items on named lists (e.g. "grocery", "todo"). Items can be marked
done (crossed off) but remain visible. Lists exist implicitly as the set of
items sharing a list_name within a conversation.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("joi.tasks")


@dataclass
class Task:
    """A single task item."""

    id: int
    conversation_id: str
    list_name: str
    item_text: str
    done: bool
    created_at: int       # ms epoch
    done_at: Optional[int]
    archived: bool


def _row_to_task(row: dict) -> Task:
    """Convert a MemoryStore row dict to a Task dataclass."""
    return Task(
        id=row["id"],
        conversation_id=row["conversation_id"],
        list_name=row["list_name"],
        item_text=row["item_text"],
        done=bool(row.get("done", 0)),
        created_at=row["created_at"],
        done_at=row.get("done_at"),
        archived=bool(row.get("archived", 0)),
    )


class TaskManager:
    """
    Manages named task lists.

    Wraps MemoryStore task methods. All operations are per-conversation
    via conversation_id.
    """

    def __init__(self, memory_store):
        self._store = memory_store

    def add(self, conversation_id: str, list_name: str, item_text: str) -> int:
        """Add an item to a named list. Returns task id."""
        task_id = self._store.add_task(conversation_id, list_name, item_text)
        logger.info("Task added", extra={
            "task_id": task_id,
            "conversation_id": conversation_id,
            "list_name": list_name,
            "action": "task_add",
        })
        return task_id

    def get_list(self, conversation_id: str, list_name: str, include_archived: bool = False) -> List[Task]:
        """Return all items for a list, ordered by created_at."""
        return [_row_to_task(r) for r in self._store.get_tasks(conversation_id, list_name, include_archived)]

    def get_all_lists(self, conversation_id: str) -> List[str]:
        """Return distinct active list names for a conversation."""
        return self._store.get_task_lists(conversation_id)

    def mark_done(self, task_id: int) -> None:
        """Mark an item as done."""
        self._store.mark_task_done(task_id)
        logger.info("Task done", extra={"task_id": task_id, "action": "task_done"})

    def reopen(self, task_id: int) -> None:
        """Reopen a done item."""
        self._store.reopen_task(task_id)
        logger.info("Task reopened", extra={"task_id": task_id, "action": "task_reopen"})

    def archive_item(self, task_id: int) -> None:
        """Soft-delete a single item."""
        self._store.archive_task(task_id)
        logger.info("Task archived", extra={"task_id": task_id, "action": "task_archive"})

    def archive_list(self, conversation_id: str, list_name: str) -> int:
        """Soft-delete all items in a list. Returns count archived."""
        count = self._store.archive_task_list(conversation_id, list_name)
        logger.info("Task list archived", extra={
            "conversation_id": conversation_id,
            "list_name": list_name,
            "count": count,
            "action": "task_list_archive",
        })
        return count

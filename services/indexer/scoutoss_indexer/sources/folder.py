"""Folder source: every file under a directory is one work item (one instance)."""

from __future__ import annotations

import functools
from collections.abc import Iterator
from pathlib import Path

from scoutoss_indexer.pipeline import WorkItem


def _identity(path: Path) -> Path:
    return path


class FolderSource:
    name = "folder"

    def __init__(self, root: Path, project: str = "FOLDER") -> None:
        self.root = root
        self.project = project
        self._seen: dict[str, tuple[float, int]] = {}

    def _files(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                yield path

    def pending(self, *, rescan: bool = False) -> list[WorkItem]:
        items: list[WorkItem] = []
        for path in self._files():
            stat = path.stat()
            key = str(path.relative_to(self.root))
            version = (stat.st_mtime, stat.st_size)
            if not rescan and self._seen.get(key) == version:
                continue
            self._seen[key] = version
            items.append(
                WorkItem(
                    ref=f"folder:{key}",
                    load=functools.partial(_identity, path),
                    source=self.name,
                    context={"project": self.project},
                )
            )
        return items

    def forget(self) -> int:
        count = len(self._seen)
        self._seen.clear()
        return count

"""Where headers come from: the XNAT REST API (production) or a folder (tests, bulk loads)."""

from scoutoss_indexer.sources.folder import FolderSource
from scoutoss_indexer.sources.xnat import SessionWork, XnatClient, XnatSource

__all__ = ["FolderSource", "SessionWork", "XnatClient", "XnatSource"]

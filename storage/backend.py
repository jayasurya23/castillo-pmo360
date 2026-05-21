"""
Storage abstraction. Local filesystem in dev, SharePoint via Microsoft Graph
in production. The interface is dead simple — `save(path, bytes)` returns a
public-ish URL or local path you can hand to the user.

SharePoint implementation is stubbed for Phase 5. The interface is finalized
so the rest of the app already speaks to it correctly.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from config import is_local_dev, local_output_dir, sharepoint_config


class StorageBackend(ABC):
    @abstractmethod
    def save(self, relative_path: str, content: bytes) -> str:
        """Save file, return retrievable URL/path."""

    @abstractmethod
    def read(self, relative_path: str) -> bytes:
        """Read file by relative path."""

    @abstractmethod
    def list_folder(self, relative_path: str) -> list[str]:
        """List files in folder."""


class LocalFSBackend(StorageBackend):
    def __init__(self, root: Optional[Path] = None):
        self.root = root or local_output_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save(self, relative_path: str, content: bytes) -> str:
        path = self._abs(relative_path)
        path.write_bytes(content)
        return str(path)

    def read(self, relative_path: str) -> bytes:
        return self._abs(relative_path).read_bytes()

    def list_folder(self, relative_path: str) -> list[str]:
        folder = self._abs(relative_path)
        if not folder.is_dir():
            return []
        return [str(p.relative_to(self.root)) for p in folder.iterdir() if p.is_file()]


class SharePointBackend(StorageBackend):
    """
    SharePoint via Microsoft Graph. Implementation deferred to Phase 5.
    See docs/PRODUCTION_SETUP.md for the Azure AD app registration steps.
    """
    def __init__(self):
        self.config = sharepoint_config()
        for k in ("tenant_id", "client_id", "client_secret", "site_id", "drive_id"):
            if not self.config.get(k):
                raise RuntimeError(
                    f"SharePoint config missing: {k}. "
                    f"Either set LOCAL_DEV_MODE=true or fill in the AZURE_* env vars."
                )

    def save(self, relative_path: str, content: bytes) -> str:
        raise NotImplementedError(
            "SharePointBackend.save: implement in Phase 5 using msal + requests. "
            "See docs/PRODUCTION_SETUP.md."
        )

    def read(self, relative_path: str) -> bytes:
        raise NotImplementedError("SharePointBackend.read: implement in Phase 5")

    def list_folder(self, relative_path: str) -> list[str]:
        raise NotImplementedError("SharePointBackend.list_folder: implement in Phase 5")


def get_storage() -> StorageBackend:
    """Returns the configured storage backend."""
    if is_local_dev():
        return LocalFSBackend()
    return SharePointBackend()

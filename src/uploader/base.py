"""Abstract base class for cloud uploaders."""

from abc import ABC, abstractmethod
from typing import Callable, Optional, Dict, Any


class UploaderBase(ABC):
    """Abstract interface for cloud storage uploaders."""

    @abstractmethod
    def upload(
        self,
        local_path: str,
        remote_dir: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        delete_on_success: bool = False,
    ) -> Dict[str, Any]:
        """
        Upload local file to remote storage.

        :param local_path: Local filesystem path to the file.
        :param remote_dir: Target directory on the remote storage.
        :param progress_callback: Callable receiving progress dict (percentage, speed, bytes_transferred, etc.).
        :param is_cancelled: Callable returning True if upload should be aborted.
        :param delete_on_success: If True, delete or move local file upon successful upload.
        :return: Result dict with {"success": bool, "error": str, ...}.
        """
        pass

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """
        Test connection to the remote storage.

        :return: Dict with {"ok": bool, "message": str, "details": dict}.
        """
        pass

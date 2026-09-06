"""Google Drive / rclone configuration helper."""

import os
import shutil
from dataclasses import dataclass
from typing import Optional, List


def _clean_remote_name(remote: str) -> str:
    """Ensure remote name has no trailing colon for internal consistency."""
    remote = (remote or "").strip()
    return remote[:-1] if remote.endswith(":") else remote


@dataclass
class UploaderConfig:
    enabled: bool = False
    rclone_binary: str = "rclone"
    rclone_config: str = ""
    remote_name: str = "gdrive"
    target_dir: str = "TG_Downloads"
    auto_upload: bool = False
    delete_local_after_upload: bool = False
    max_concurrent_uploads: int = 1
    bwlimit: str = ""

    @classmethod
    def from_env(cls) -> "UploaderConfig":
        import config
        return cls(
            enabled=getattr(config, "GDRIVE_ENABLED", False),
            rclone_binary=getattr(config, "GDRIVE_RCLONE_BINARY", "rclone"),
            rclone_config=getattr(config, "GDRIVE_RCLONE_CONFIG", ""),
            remote_name=_clean_remote_name(getattr(config, "GDRIVE_RCLONE_REMOTE", "gdrive")),
            target_dir=getattr(config, "GDRIVE_TARGET_DIR", "TG_Downloads"),
            auto_upload=getattr(config, "GDRIVE_AUTO_UPLOAD", False),
            delete_local_after_upload=getattr(config, "GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", False),
            max_concurrent_uploads=max(1, getattr(config, "GDRIVE_MAX_CONCURRENT_UPLOADS", 1)),
            bwlimit=getattr(config, "GDRIVE_BWLIMIT", ""),
        )

    def resolve_binary(self) -> Optional[str]:
        """Find the absolute path of the rclone binary."""
        if os.path.isabs(self.rclone_binary):
            return self.rclone_binary if os.path.isfile(self.rclone_binary) and os.access(self.rclone_binary, os.X_OK) else None
        return shutil.which(self.rclone_binary)
    def resolve_config_path(self) -> str:
        """Resolve the effective rclone.conf path."""
        if self.rclone_config:
            return os.path.abspath(self.rclone_config)
        default_system = os.path.expanduser("~/.config/rclone/rclone.conf")
        if os.path.isfile(default_system):
            return default_system
        return os.path.abspath(".task_state/rclone.conf")


    def format_remote_path(self, sub_dir: Optional[str] = None) -> str:
        """
        Format the rclone remote destination string.
        E.g. 'gdrive:TG_Downloads' or 'gdrive:TG_Downloads/MyChannel'
        """
        remote = _clean_remote_name(self.remote_name)
        base = self.target_dir.strip("/\\")
        if sub_dir:
            clean_sub = sub_dir.strip("/\\")
            if clean_sub:
                base = f"{base}/{clean_sub}" if base else clean_sub
        return f"{remote}:{base}" if base else f"{remote}:"

    def validate(self) -> List[str]:
        """Validate configuration sanity, returning a list of error strings."""
        errors = []
        if not self.remote_name:
            errors.append("GDRIVE_RCLONE_REMOTE 不能为空")
        if self.rclone_config and not os.path.isfile(self.rclone_config):
            errors.append(f"指定的 rclone 配置文件不存在: {self.rclone_config}")
        return errors

"""Google Drive rclone authorization and configuration management."""

import configparser
import json
import logging
import os
import re
import shutil
from typing import Dict, Any, Optional

logger = logging.getLogger("tg_downloader.uploader.auth")


def extract_token_json(raw_input: str) -> Optional[Dict[str, Any]]:
    """
    Extract and validate a token JSON object from raw input.
    Input can be a plain JSON string or console output containing '{...}'.
    """
    if not raw_input:
        return None
    raw = raw_input.strip()

    # Try direct parse
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and ("access_token" in parsed or "refresh_token" in parsed):
            return parsed
    except Exception:
        pass

    # Try finding JSON block {...}
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and ("access_token" in parsed or "refresh_token" in parsed):
                return parsed
        except Exception:
            pass

    return None


def read_rclone_config(config_path: str) -> configparser.ConfigParser:
    """Read rclone.conf into a ConfigParser instance."""
    parser = configparser.ConfigParser()
    if os.path.isfile(config_path):
        try:
            parser.read(config_path, encoding="utf-8")
        except Exception as exc:
            logger.warning(f"读取 rclone 配置异常: {exc}")
    return parser


def write_rclone_config(config_path: str, parser: configparser.ConfigParser):
    """Write ConfigParser to rclone.conf safely with restricted permissions."""
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        parser.write(f, space_around_delimiters=True)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, config_path)


class RcloneAuthManager:
    """Manages remote configuration entries in rclone.conf."""

    def __init__(self, config_path: str, remote_name: str = "gdrive", binary_path: Optional[str] = None):
        self.config_path = config_path
        self.remote_name = remote_name
        self.binary_path = binary_path or shutil.which("rclone")

    def get_info(self) -> Dict[str, Any]:
        """Get remote configuration info and rclone system status."""
        is_installed = bool(self.binary_path and os.path.exists(self.binary_path))
        parser = read_rclone_config(self.config_path)
        configured = parser.has_section(self.remote_name)

        token_expiry = None
        has_refresh_token = False
        scope = ""
        backend_type = ""

        if configured:
            backend_type = parser.get(self.remote_name, "type", fallback="")
            scope = parser.get(self.remote_name, "scope", fallback="")
            raw_token = parser.get(self.remote_name, "token", fallback="")
            if raw_token:
                try:
                    tok = json.loads(raw_token)
                    token_expiry = tok.get("expiry")
                    has_refresh_token = bool(tok.get("refresh_token"))
                except Exception:
                    pass

        return {
            "remote_name": self.remote_name,
            "config_path": self.config_path,
            "installed": is_installed,
            "binary_path": self.binary_path,
            "configured": configured,
            "backend_type": backend_type,
            "scope": scope,
            "has_refresh_token": has_refresh_token,
            "token_expiry": token_expiry,
            "helper_command": 'rclone authorize "drive"',
        }

    def save_token(
        self,
        token_input: str,
        scope: str = "drive",
        team_drive_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Save a Google Drive OAuth token to rclone.conf.
        """
        token_data = extract_token_json(token_input)
        if not token_data:
            return {
                "ok": False,
                "error": "输入的 Token 无效，必须包含 access_token 或 refresh_token 的 JSON 对象",
            }

        parser = read_rclone_config(self.config_path)
        if not parser.has_section(self.remote_name):
            parser.add_section(self.remote_name)

        parser.set(self.remote_name, "type", "drive")
        parser.set(self.remote_name, "scope", scope or "drive")
        parser.set(self.remote_name, "token", json.dumps(token_data))

        if team_drive_id:
            parser.set(self.remote_name, "team_drive", team_drive_id.strip())

        try:
            write_rclone_config(self.config_path, parser)
            logger.info(f"成功保存 Google Drive 凭据至 {self.config_path} [{self.remote_name}]")
            return {
                "ok": True,
                "remote_name": self.remote_name,
                "message": f"已成功保存 Google Drive remote '{self.remote_name}'",
            }
        except Exception as exc:
            logger.error(f"保存 rclone 凭据失败: {exc}")
            return {"ok": False, "error": f"写入配置文件失败: {exc}"}

    def save_raw_config(self, raw_content: str) -> Dict[str, Any]:
        """Directly write raw rclone.conf content with syntax validation."""
        test_parser = configparser.ConfigParser()
        try:
            test_parser.read_string(raw_content)
        except Exception as exc:
            return {"ok": False, "error": f"配置文件格式非法: {exc}"}

        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(raw_content)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
            return {"ok": True, "message": "已成功更新 rclone.conf"}
        except Exception as exc:
            return {"ok": False, "error": f"写入文件失败: {exc}"}

    def unbind(self) -> Dict[str, Any]:
        """Remove the configured remote from rclone.conf."""
        parser = read_rclone_config(self.config_path)
        if parser.has_section(self.remote_name):
            parser.remove_section(self.remote_name)
            try:
                write_rclone_config(self.config_path, parser)
                return {"ok": True, "message": f"已解绑 remote '{self.remote_name}'"}
            except Exception as exc:
                return {"ok": False, "error": f"保存文件失败: {exc}"}
        return {"ok": True, "message": "未配置该 remote，无需解绑"}

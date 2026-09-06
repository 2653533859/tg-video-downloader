"""Unit tests for RcloneUploader and uploader configuration."""

import json
import os
import subprocess
import pytest
from unittest.mock import Mock, patch, MagicMock

from src.uploader.config import UploaderConfig, _clean_remote_name
from src.uploader.rclone import RcloneUploader, parse_rclone_json_line, format_speed


class TestUploaderConfig:
    def test_clean_remote_name(self):
        assert _clean_remote_name("gdrive:") == "gdrive"
        assert _clean_remote_name("gdrive") == "gdrive"
        assert _clean_remote_name(" my_remote: ") == "my_remote"
        assert _clean_remote_name("") == ""

    def test_format_remote_path(self):
        cfg = UploaderConfig(remote_name="gdrive", target_dir="TG_Downloads")
        assert cfg.format_remote_path() == "gdrive:TG_Downloads"
        assert cfg.format_remote_path("Channel_A") == "gdrive:TG_Downloads/Channel_A"
        assert cfg.format_remote_path("/Sub/Dir/") == "gdrive:TG_Downloads/Sub/Dir"

        # Edge case: empty target_dir
        cfg_root = UploaderConfig(remote_name="gdrive", target_dir="")
        assert cfg_root.format_remote_path() == "gdrive:"
        assert cfg_root.format_remote_path("Sub") == "gdrive:Sub"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("GDRIVE_ENABLED", "true")
        monkeypatch.setenv("GDRIVE_RCLONE_REMOTE", "mydrive:")
        monkeypatch.setenv("GDRIVE_TARGET_DIR", "Videos")
        monkeypatch.setenv("GDRIVE_AUTO_UPLOAD", "true")
        monkeypatch.setenv("GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", "true")
        monkeypatch.setenv("GDRIVE_BWLIMIT", "10M")

        # Reload config module
        import config
        monkeypatch.setattr(config, "GDRIVE_ENABLED", True)
        monkeypatch.setattr(config, "GDRIVE_RCLONE_REMOTE", "mydrive:")
        monkeypatch.setattr(config, "GDRIVE_TARGET_DIR", "Videos")
        monkeypatch.setattr(config, "GDRIVE_AUTO_UPLOAD", True)
        monkeypatch.setattr(config, "GDRIVE_DELETE_LOCAL_AFTER_UPLOAD", True)
        monkeypatch.setattr(config, "GDRIVE_BWLIMIT", "10M")

        cfg = UploaderConfig.from_env()
        assert cfg.enabled is True
        assert cfg.remote_name == "mydrive"
        assert cfg.target_dir == "Videos"
        assert cfg.auto_upload is True
        assert cfg.delete_local_after_upload is True
        assert cfg.bwlimit == "10M"

    def test_validate(self, tmp_path):
        cfg = UploaderConfig(remote_name="")
        errs = cfg.validate()
        assert any("GDRIVE_RCLONE_REMOTE 不能为空" in e for e in errs)

        cfg_nonexist = UploaderConfig(remote_name="gd", rclone_config=str(tmp_path / "nonexist.conf"))
        errs2 = cfg_nonexist.validate()
        assert any("不存在" in e for e in errs2)


class TestRcloneJsonParser:
    def test_parse_valid_stats_line(self):
        line = json.dumps({
            "level": "info",
            "msg": "Transferred stats",
            "stats": {
                "bytes": 52428800,
                "totalBytes": 104857600,
                "percentage": 50,
                "speed": 10485760,
                "eta": 5,
            }
        })
        res = parse_rclone_json_line(line)
        assert res is not None
        assert res["percentage"] == 50.0
        assert res["bytes"] == 52428800
        assert res["total_bytes"] == 104857600
        assert res["speed_bps"] == 10485760.0
        assert "MB/s" in res["speed"]
        assert res["eta"] == 5

    def test_parse_percentage_calculation_fallback(self):
        line = json.dumps({
            "stats": {
                "bytes": 25,
                "totalBytes": 100,
                "speed": 0,
            }
        })
        res = parse_rclone_json_line(line)
        assert res is not None
        assert res["percentage"] == 25.0

    def test_parse_non_stats_or_invalid_json(self):
        assert parse_rclone_json_line("not json") is None
        assert parse_rclone_json_line(json.dumps({"level": "info", "msg": "Starting"})) is None
        assert parse_rclone_json_line("") is None


class TestRcloneUploader:
    def test_build_command(self):
        cfg = UploaderConfig(
            rclone_binary="/usr/bin/rclone",
            remote_name="gdrive",
            target_dir="Backup",
            rclone_config="/path/to/rclone.conf",
            bwlimit="5M",
        )
        uploader = RcloneUploader(cfg)

        # Copy mode
        cmd_copy = uploader.build_command("/tmp/file.mp4", "gdrive:Backup", delete_on_success=False)
        assert cmd_copy[0] == "/usr/bin/rclone"
        assert cmd_copy[1] == "copy"
        assert cmd_copy[2] == "/tmp/file.mp4"
        assert cmd_copy[3] == "gdrive:Backup"
        assert "--use-json-log" in cmd_copy
        assert "--config" in cmd_copy and "/path/to/rclone.conf" in cmd_copy
        assert "--bwlimit" in cmd_copy and "5M" in cmd_copy

        # Move mode (delete_on_success)
        cmd_move = uploader.build_command("/tmp/file.mp4", "gdrive:Backup", delete_on_success=True)
        assert cmd_move[1] == "move"

    def test_upload_nonexistent_local_file(self):
        uploader = RcloneUploader()
        res = uploader.upload("/nonexistent/file.mp4", "gdrive:test")
        assert res["success"] is False
        assert "本地文件不存在" in res["error"]

    def test_upload_missing_rclone_binary(self, tmp_path):
        dummy_file = tmp_path / "video.mp4"
        dummy_file.write_text("content")

        cfg = UploaderConfig(rclone_binary="/path/to/nonexistent/rclone_binary_xyz")
        uploader = RcloneUploader(cfg)
        res = uploader.upload(str(dummy_file), "gdrive:test")
        assert res["success"] is False
        assert "找不到 rclone 可执行文件" in res["error"]

    @patch("src.uploader.rclone.subprocess.Popen")
    def test_upload_success_with_progress(self, mock_popen, tmp_path):
        dummy_file = tmp_path / "video.mp4"
        dummy_file.write_text("dummy video content")

        mock_process = MagicMock()
        poll_count = 0
        def fake_poll():
            nonlocal poll_count
            poll_count += 1
            return None if poll_count < 3 else 0
        mock_process.poll.side_effect = fake_poll
        mock_process.returncode = 0
        stats_line = json.dumps({
            "stats": {"bytes": 100, "totalBytes": 100, "percentage": 100, "speed": 5000}
        }) + "\n"
        mock_process.stderr = [stats_line]
        mock_popen.return_value = mock_process

        cfg = UploaderConfig(rclone_binary="rclone")
        with patch.object(cfg, "resolve_binary", return_value="/mock/rclone"):
            uploader = RcloneUploader(cfg)
            progress_updates = []

            result = uploader.upload(
                str(dummy_file),
                "MyChannel",
                progress_callback=lambda p: progress_updates.append(p),
                delete_on_success=False,
            )

            assert result["success"] is True
            assert result["error"] == ""
            assert len(progress_updates) > 0
            assert progress_updates[-1]["percentage"] == 100.0

    @patch("src.uploader.rclone.subprocess.Popen")
    def test_upload_cancelled(self, mock_popen, tmp_path):
        dummy_file = tmp_path / "video.mp4"
        dummy_file.write_text("dummy")

        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stderr = []
        mock_popen.return_value = mock_process

        cfg = UploaderConfig(rclone_binary="rclone")
        with patch.object(cfg, "resolve_binary", return_value="/mock/rclone"):
            uploader = RcloneUploader(cfg)
            is_cancelling = [False]

            def cancel_checker():
                return is_cancelling[0]

            is_cancelling[0] = True
            result = uploader.upload(
                str(dummy_file),
                "MyChannel",
                is_cancelled=cancel_checker,
            )

            assert result["success"] is False
            assert result.get("cancelled") is True
            mock_process.terminate.assert_called()

    @patch("subprocess.run")
    def test_test_connection_success(self, mock_run):
        cfg = UploaderConfig(rclone_binary="rclone", remote_name="gdrive")
        with patch.object(cfg, "resolve_binary", return_value="/mock/rclone"):
            uploader = RcloneUploader(cfg)

            # 1. version call, 2. about call
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="rclone v1.65.0\n"),
                MagicMock(returncode=0, stdout="Total: 15GB\nUsed: 5GB\n"),
            ]

            status = uploader.test_connection()
            assert status["ok"] is True
            assert "成功连接" in status["message"]
            assert status["details"]["installed"] is True

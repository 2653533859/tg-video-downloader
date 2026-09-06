"""Unit tests for RcloneAuthManager and auth routes."""

import json
import os
import pytest
from flask import Flask
from unittest.mock import Mock

from src.uploader.auth import (
    extract_token_json,
    RcloneAuthManager,
    read_rclone_config,
)
from src.routes.uploader import bp as uploader_bp, init_blueprint


class TestExtractTokenJson:
    def test_direct_valid_token(self):
        token_str = json.dumps({
            "access_token": "ya29.xyz",
            "token_type": "Bearer",
            "refresh_token": "1//abc",
            "expiry": "2026-09-06T15:00:00Z",
        })
        parsed = extract_token_json(token_str)
        assert parsed is not None
        assert parsed["access_token"] == "ya29.xyz"
        assert parsed["refresh_token"] == "1//abc"

    def test_extracted_from_console_output(self):
        output = """
        Paste the following into your remote machine:
        {"access_token":"ya29.console","token_type":"Bearer","refresh_token":"1//console","expiry":"2026-09-06T15:00:00Z"}
        End of token.
        """
        parsed = extract_token_json(output)
        assert parsed is not None
        assert parsed["access_token"] == "ya29.console"

    def test_invalid_input(self):
        assert extract_token_json("") is None
        assert extract_token_json("some random text without json") is None
        assert extract_token_json('{"foo": "bar"}') is None  # no access/refresh token


class TestRcloneAuthManager:
    def test_save_token_and_get_info(self, tmp_path):
        conf_file = str(tmp_path / "rclone.conf")
        mgr = RcloneAuthManager(config_path=conf_file, remote_name="gdrive")

        info_before = mgr.get_info()
        assert info_before["configured"] is False

        token_str = json.dumps({
            "access_token": "ya29.test",
            "refresh_token": "1//test",
            "expiry": "2026-10-01T00:00:00Z",
        })

        res = mgr.save_token(token_str, scope="drive.readonly", team_drive_id="team_123")
        assert res["ok"] is True

        info_after = mgr.get_info()
        assert info_after["configured"] is True
        assert info_after["backend_type"] == "drive"
        assert info_after["scope"] == "drive.readonly"
        assert info_after["has_refresh_token"] is True
        assert info_after["token_expiry"] == "2026-10-01T00:00:00Z"

        parser = read_rclone_config(conf_file)
        assert parser.has_section("gdrive")
        assert parser.get("gdrive", "team_drive") == "team_123"

        # Unbind
        unbind_res = mgr.unbind()
        assert unbind_res["ok"] is True
        info_unbound = mgr.get_info()
        assert info_unbound["configured"] is False

    def test_save_raw_config(self, tmp_path):
        conf_file = str(tmp_path / "rclone.conf")
        mgr = RcloneAuthManager(config_path=conf_file, remote_name="gdrive")

        # Invalid config
        res_bad = mgr.save_raw_config("invalid text with no sections")
        assert res_bad["ok"] is False

        # Valid config
        valid_ini = "[gdrive]\ntype = drive\ntoken = {\"access_token\":\"123\"}\n"
        res_good = mgr.save_raw_config(valid_ini)
        assert res_good["ok"] is True
        assert mgr.get_info()["configured"] is True


@pytest.fixture
def auth_client():
    app = Flask(__name__)
    mock_upload_manager = Mock()
    mock_auth = Mock()
    mock_upload_manager.auth = mock_auth

    init_blueprint({
        "upload_manager": mock_upload_manager,
        "resolve_download_path": lambda *p, **kw: "/mock/path",
    })
    app.register_blueprint(uploader_bp)

    app.config["TESTING"] = True
    with app.test_client() as client:
        client.mock_auth = mock_auth
        yield client


def test_auth_routes(auth_client):
    # GET /api/gdrive/auth/info
    auth_client.mock_auth.get_info.return_value = {
        "remote_name": "gdrive",
        "configured": False,
        "installed": True,
    }
    res = auth_client.get("/api/gdrive/auth/info")
    assert res.status_code == 200
    assert res.get_json()["configured"] is False

    # POST /api/gdrive/auth/save_token missing token
    res_err = auth_client.post("/api/gdrive/auth/save_token", json={})
    assert res_err.status_code == 400

    # POST /api/gdrive/auth/save_token success
    auth_client.mock_auth.save_token.return_value = {"ok": True, "remote_name": "gdrive"}
    res_ok = auth_client.post("/api/gdrive/auth/save_token", json={"token": '{"access_token":"test"}'})
    assert res_ok.status_code == 200
    assert res_ok.get_json()["ok"] is True

    # POST /api/gdrive/auth/unbind
    auth_client.mock_auth.unbind.return_value = {"ok": True}
    res_unbind = auth_client.post("/api/gdrive/auth/unbind")
    assert res_unbind.status_code == 200
    assert res_unbind.get_json()["ok"] is True

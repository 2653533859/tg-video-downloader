"""Unit tests for uploader Blueprint API routes."""

import pytest
from flask import Flask
from unittest.mock import Mock, patch

from src.routes.uploader import bp as uploader_bp, init_blueprint


@pytest.fixture
def client():
    app = Flask(__name__)
    mock_upload_manager = Mock()
    mock_upload_manager.auth.get_info.return_value = {"configured": True}
    mock_resolve_path = Mock()
    init_blueprint({
        "upload_manager": mock_upload_manager,
        "resolve_download_path": mock_resolve_path,
    })
    app.register_blueprint(uploader_bp)

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        test_client.mock_upload_manager = mock_upload_manager
        test_client.mock_resolve_path = mock_resolve_path
        yield test_client


def test_status_endpoint(client):
    client.mock_upload_manager.get_status_payload.return_value = {
        "enabled": True,
        "queue_count": 0,
        "queue": [],
    }
    res = client.get("/api/gdrive/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["enabled"] is True
    assert data["queue_count"] == 0


def test_upload_missing_parameters(client):
    res = client.post("/api/gdrive/upload", json={})
    assert res.status_code == 400
    assert "缺少 folder 或 filename" in res.get_json()["error"]


def test_upload_unconfigured_error(client):
    client.mock_upload_manager.auth.get_info.return_value = {"configured": False}
    res = client.post("/api/gdrive/upload", json={"folder": "ch", "filename": "v.mp4"})
    assert res.status_code == 400
    assert "尚未绑定 Google 云盘" in res.get_json()["error"]

def test_upload_file_not_found(client):
    client.mock_resolve_path.side_effect = FileNotFoundError("文件未找到")
    res = client.post("/api/gdrive/upload", json={"folder": "ch", "filename": "video.mp4"})
    assert res.status_code == 404
    assert "文件未找到" in res.get_json()["error"]


def test_upload_success(client):
    client.mock_resolve_path.return_value = "/downloads/ch/video.mp4"
    client.mock_upload_manager.enqueue.return_value = {
        "upload_id": "up_123",
        "status": "pending",
        "filename": "video.mp4",
    }
    res = client.post("/api/gdrive/upload", json={
        "folder": "ch",
        "filename": "video.mp4",
        "delete_local": True,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["task"]["upload_id"] == "up_123"
    client.mock_upload_manager.enqueue.assert_called_once_with(
        filepath="/downloads/ch/video.mp4",
        folder="ch",
        filename="video.mp4",
        delete_local=True,
    )


def test_cancel_endpoint(client):
    # Missing param
    res1 = client.post("/api/gdrive/cancel", json={})
    assert res1.status_code == 400

    # Success
    client.mock_upload_manager.cancel.return_value = True
    res2 = client.post("/api/gdrive/cancel", json={"upload_id": "up_1"})
    assert res2.status_code == 200
    assert res2.get_json()["ok"] is True
    client.mock_upload_manager.cancel.assert_called_once_with("up_1")


def test_retry_endpoint(client):
    # Missing param
    res1 = client.post("/api/gdrive/retry", json={})
    assert res1.status_code == 400

    # Not found
    client.mock_upload_manager.retry.return_value = None
    res2 = client.post("/api/gdrive/retry", json={"upload_id": "up_none"})
    assert res2.status_code == 404

    # Success
    client.mock_upload_manager.retry.return_value = {"upload_id": "up_1", "status": "pending"}
    res3 = client.post("/api/gdrive/retry", json={"upload_id": "up_1"})
    assert res3.status_code == 200
    assert res3.get_json()["ok"] is True


def test_test_endpoint(client):
    client.mock_upload_manager.uploader.test_connection.return_value = {
        "ok": True,
        "message": "Connected",
    }
    res = client.get("/api/gdrive/test")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

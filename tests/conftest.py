"""
测试配置和共享 fixtures
"""
import os
import pytest
import tempfile
import shutil
from unittest.mock import Mock, MagicMock


@pytest.fixture
def temp_download_dir():
    """创建临时下载目录"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_config(monkeypatch, temp_download_dir):
    """Mock 配置环境变量"""
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "test_hash")
    monkeypatch.setenv("DOWNLOAD_DIR", temp_download_dir)
    monkeypatch.setenv("WEB_AUTH_USERNAME", "test_user")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "test_pass")
    monkeypatch.setenv("WEB_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_BIND_PORT", "5000")
    monkeypatch.setenv("TG_PROXY_ENABLED", "false")


@pytest.fixture
def mock_telegram_client():
    """Mock Telethon Client"""
    client = MagicMock()
    client.connect = Mock(return_value=None)
    client.is_user_authorized = Mock(return_value=True)
    client.get_me = Mock(return_value=Mock(
        id=123456,
        first_name="Test",
        last_name="User",
        username="testuser"
    ))
    return client

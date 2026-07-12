"""
测试 relay_tokens 模块
"""
import pytest
import time
from relay_tokens import build_relay_token, verify_relay_token


class TestRelayTokens:
    """Relay Token 生成和验证测试"""

    def test_build_token(self):
        """测试生成 token"""
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=1700000000
        )

        assert isinstance(token, str)
        assert "." in token
        parts = token.split(".")
        assert len(parts) == 2
        assert parts[1] == "1700000000"

    def test_verify_valid_token(self):
        """测试验证有效 token"""
        expire_at = int(time.time()) + 3600
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        # 不应抛出异常
        verify_relay_token(
            secret="test_secret",
            token=token,
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            now_ts=int(time.time())
        )

    def test_verify_expired_token(self):
        """测试验证过期 token"""
        expire_at = int(time.time()) - 3600  # 1小时前过期
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="test_secret",
                token=token,
                entity_id=123456,
                message_id=789,
                file_name="video.mp4",
                now_ts=int(time.time())
            )

        assert "expired" in str(exc_info.value).lower()

    def test_verify_invalid_signature(self):
        """测试验证无效签名"""
        expire_at = int(time.time()) + 3600
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        # 使用不同的密钥验证
        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="wrong_secret",
                token=token,
                entity_id=123456,
                message_id=789,
                file_name="video.mp4",
                now_ts=int(time.time())
            )

        assert "signature" in str(exc_info.value).lower()

    def test_verify_tampered_entity_id(self):
        """测试验证被篡改的 entity_id"""
        expire_at = int(time.time()) + 3600
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        # 使用不同的 entity_id 验证
        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="test_secret",
                token=token,
                entity_id=999999,  # 篡改
                message_id=789,
                file_name="video.mp4",
                now_ts=int(time.time())
            )

        assert "signature" in str(exc_info.value).lower()

    def test_verify_tampered_file_name(self):
        """测试验证被篡改的文件名"""
        expire_at = int(time.time()) + 3600
        token = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        # 使用不同的文件名验证
        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="test_secret",
                token=token,
                entity_id=123456,
                message_id=789,
                file_name="malicious.mp4",  # 篡改
                now_ts=int(time.time())
            )

        assert "signature" in str(exc_info.value).lower()

    def test_verify_invalid_token_format(self):
        """测试验证格式错误的 token"""
        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="test_secret",
                token="invalid_token_without_dot",
                entity_id=123456,
                message_id=789,
                file_name="video.mp4",
                now_ts=int(time.time())
            )

        assert "format" in str(exc_info.value).lower()

    def test_verify_invalid_expiration_format(self):
        """测试验证过期时间格式错误"""
        with pytest.raises(ValueError) as exc_info:
            verify_relay_token(
                secret="test_secret",
                token="signature.not_a_number",
                entity_id=123456,
                message_id=789,
                file_name="video.mp4",
                now_ts=int(time.time())
            )

        assert "expiration" in str(exc_info.value).lower()

    def test_token_uniqueness_for_different_params(self):
        """测试不同参数生成不同 token"""
        expire_at = int(time.time()) + 3600

        token1 = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video1.mp4",
            expire_at=expire_at
        )

        token2 = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video2.mp4",  # 不同文件名
            expire_at=expire_at
        )

        assert token1 != token2

    def test_token_consistency(self):
        """测试相同参数生成相同 token"""
        expire_at = 1700000000

        token1 = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        token2 = build_relay_token(
            secret="test_secret",
            entity_id=123456,
            message_id=789,
            file_name="video.mp4",
            expire_at=expire_at
        )

        assert token1 == token2

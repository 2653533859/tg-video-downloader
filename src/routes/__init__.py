"""
路由模块
包含所有 Flask Blueprint
"""
from .files import bp as files_bp
from .system import bp as system_bp
from .telegram import bp as telegram_bp
from .download import bp as download_bp
from .misc import bp as misc_bp
from .relay import bp as relay_bp
from .auth import bp as auth_bp

__all__ = [
    'files_bp',
    'system_bp',
    'telegram_bp',
    'download_bp',
    'misc_bp',
    'relay_bp',
    'auth_bp'
]

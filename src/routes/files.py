"""
文件操作路由 Blueprint
包含文件管理相关的 API
"""
from flask import Blueprint, jsonify, request, render_template

from src.files import (
    delete_download_file,
    prepare_open_folder,
    rename_download_file,
)

bp = Blueprint('files', __name__)


# 这些函数需要从主 app 注入
_resolve_download_path = None
_request_ip_is_local = None
OPEN_FOLDER_ENABLED = None


def init_blueprint(resolve_path_func, is_local_func, open_folder_enabled):
    """
    初始化 Blueprint 依赖

    Args:
        resolve_path_func: 路径解析函数
        is_local_func: 本地 IP 检查函数
        open_folder_enabled: 是否启用文件夹打开功能
    """
    global _resolve_download_path, _request_ip_is_local, OPEN_FOLDER_ENABLED
    _resolve_download_path = resolve_path_func
    _request_ip_is_local = is_local_func
    OPEN_FOLDER_ENABLED = open_folder_enabled


@bp.route("/")
def index():
    """首页"""
    return render_template("index.html")


@bp.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """打开本地文件夹"""
    folder = (request.json or {}).get("folder", "")
    if not folder:
        return jsonify({"error": "Missing folder"}), 400

    try:
        payload, status_code = prepare_open_folder(
            _resolve_download_path,
            folder,
            OPEN_FOLDER_ENABLED,
            _request_ip_is_local(),
        )
        return jsonify(payload), status_code

    except FileNotFoundError:
        return jsonify({"error": "Folder not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/rename-file", methods=["POST"])
def api_rename_file():
    """重命名文件"""
    data = request.json or {}
    folder = data.get("folder")
    old_name = data.get("old_name")
    new_name = data.get("new_name")

    if not all([folder, old_name, new_name]):
        return jsonify({"error": "Missing parameters"}), 400

    try:
        rename_download_file(_resolve_download_path, folder, old_name, new_name)
        return jsonify({"ok": True})

    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except FileExistsError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    """删除文件"""
    data = request.json or {}
    folder = data.get("folder")
    filename = data.get("filename")

    if not all([folder, filename]):
        return jsonify({"error": "Missing parameters"}), 400

    try:
        delete_download_file(_resolve_download_path, folder, filename)
        return jsonify({"ok": True})

    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

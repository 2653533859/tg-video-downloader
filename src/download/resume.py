"""Resume metadata storage and offset detection."""

import json
import os


class ResumeStore:
    def __init__(self, resume_dir, progress_path_func):
        self.resume_dir = resume_dir
        self.progress_path_func = progress_path_func
        os.makedirs(self.resume_dir, exist_ok=True)

    def path_for(self, task_id):
        return os.path.join(self.resume_dir, f"{task_id}.json")

    def save(self, task_id, info):
        try:
            with open(self.path_for(task_id), "w", encoding="utf-8") as handle:
                json.dump(info, handle)
        except Exception:
            pass

    def load(self, task_id):
        try:
            path = self.path_for(task_id)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        except Exception:
            pass
        return None

    def clear(self, task_id):
        try:
            path = self.path_for(task_id)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def list_task_ids(self):
        try:
            return [
                name[:-5]
                for name in os.listdir(self.resume_dir)
                if name.endswith(".json")
            ]
        except FileNotFoundError:
            return []

    def count(self):
        return len(self.list_task_ids())

    def detect_offset(self, task_id, filepath, total_bytes=0):
        progress_path = self.progress_path_func(filepath)
        if os.path.exists(progress_path):
            size = os.path.getsize(progress_path)
            if size > 0 and (not total_bytes or size < total_bytes):
                return size

        resume_info = self.load(task_id) or {}
        resume_offset = int(resume_info.get("offset") or 0)
        if resume_offset > 0 and (not total_bytes or resume_offset < total_bytes):
            return resume_offset

        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size > 0 and (not total_bytes or size < total_bytes):
                return size

        return 0

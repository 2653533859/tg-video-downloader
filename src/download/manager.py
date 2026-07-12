"""Download submission manager."""

import os


class DownloadManager:
    """Selects the downloader and submits work to the queue processor."""

    def __init__(
        self,
        *,
        tdl_binary,
        supports_tdl_download,
        add_to_queue,
        process_queue,
    ):
        self.tdl_binary = tdl_binary
        self.supports_tdl_download = supports_tdl_download
        self.add_to_queue = add_to_queue
        self.process_queue = process_queue

    def enqueue_tdl(self, task_id, entity_id, msg_id, dialog_name, info):
        if not os.path.exists(self.tdl_binary):
            raise RuntimeError(f"tdl 不存在: {self.tdl_binary}")
        task = {
            "task_id": task_id,
            "entity_id": entity_id,
            "msg_id": msg_id,
            "dialog_name": dialog_name,
            "info": info,
            "downloader": "tdl",
        }
        self.add_to_queue(task)
        self.process_queue()
        return task_id

    def enqueue_telegram(self, task_id, entity_id, msg_id, dialog_name, info):
        task = {
            "task_id": task_id,
            "entity_id": entity_id,
            "msg_id": msg_id,
            "dialog_name": dialog_name,
            "info": info,
            "downloader": "telegram",
        }
        self.add_to_queue(task)
        self.process_queue()
        return task_id

    def enqueue(self, task_id, entity_id, msg_id, dialog_name, info):
        if self.supports_tdl_download(entity_id):
            return self.enqueue_tdl(task_id, entity_id, msg_id, dialog_name, info)
        return self.enqueue_telegram(task_id, entity_id, msg_id, dialog_name, info)

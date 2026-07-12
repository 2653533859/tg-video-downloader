"""Telegram debug inspection services."""


class TelegramDebugService:
    def __init__(self, client, run_async, dialogs_cache, cache_lock):
        self.client = client
        self.run_async = run_async
        self.dialogs_cache = dialogs_cache
        self.cache_lock = cache_lock

    def _dialog_entity(self, dialog_index):
        if dialog_index is None:
            return None
        with self.cache_lock:
            if 0 <= dialog_index < len(self.dialogs_cache):
                return self.dialogs_cache[dialog_index].entity
        return None

    def inspect_messages(self, dialog_index, limit=20, reply_to=None):
        entity = self._dialog_entity(dialog_index)
        if entity is None:
            return {"error": "invalid dialog_index"}, 200

        async def scan():
            results = []
            kwargs = {"limit": limit}
            if reply_to is not None:
                kwargs["reply_to"] = reply_to

            async for msg in self.client.iter_messages(entity, **kwargs):
                item = {
                    "id": msg.id,
                    "text": msg.text[:50] if msg.text else None,
                    "media_type": None,
                    "doc_mime": None,
                    "doc_size": None,
                    "attrs": [],
                }
                if msg.media:
                    item["media_type"] = type(msg.media).__name__
                    if hasattr(msg.media, "document"):
                        doc = msg.media.document
                        item["doc_mime"] = doc.mime_type
                        item["doc_size"] = doc.size
                        item["attrs"] = [type(attr).__name__ for attr in doc.attributes]
                        if reply_to is None:
                            item["attr_details"] = self._attribute_details(doc.attributes)
                results.append(item)
            return results

        return self.run_async(scan), 200

    def inspect_full_messages(self, dialog_index):
        entity = self._dialog_entity(dialog_index)
        if entity is None:
            return {"error": "invalid dialog_index"}, 200

        async def scan():
            results = []
            async for msg in self.client.iter_messages(entity, limit=5):
                reply_markup = str(msg.reply_markup) if getattr(msg, "reply_markup", None) else None
                results.append({
                    "id": msg.id,
                    "text": msg.text,
                    "reply_markup": reply_markup,
                    "entities": [str(entity) for entity in (msg.entities or [])],
                })
            return results

        return self.run_async(scan), 200

    @staticmethod
    def _attribute_details(attributes):
        details = []
        for attr in attributes:
            if hasattr(attr, "file_name"):
                details.append({"file_name": attr.file_name})
            elif hasattr(attr, "duration"):
                details.append({"video_duration": attr.duration})
        return details

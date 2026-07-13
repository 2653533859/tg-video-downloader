"""Telegram video scanning services."""

import time


class TelegramVideoService:
    def __init__(
        self,
        client,
        run_async,
        resolve_requested_entity,
        video_info_for_message,
        message_text,
        make_excerpt,
        cache_lock,
        current_entity_cache,
        videos_cache,
        replies_cache,
        max_video_cache_size=30,
        max_reply_cache_size=500,
        video_cache_ttl=300,
        reply_cache_ttl=300,
        log_warning=None,
    ):
        self.client = client
        self.run_async = run_async
        self.resolve_requested_entity = resolve_requested_entity
        self.video_info_for_message = video_info_for_message
        self.message_text = message_text
        self.make_excerpt = make_excerpt
        self.cache_lock = cache_lock
        self.current_entity_cache = current_entity_cache
        self.videos_cache = videos_cache
        self.replies_cache = replies_cache
        self.max_video_cache_size = max_video_cache_size
        self.max_reply_cache_size = max_reply_cache_size
        self.video_cache_ttl = video_cache_ttl
        self.reply_cache_ttl = reply_cache_ttl
        self.log_warning = log_warning or (lambda message: None)

    @staticmethod
    def _cache_fresh(entry, ttl):
        """条目是否在 TTL 内；ttl<=0 视为永不过期（关闭 TTL）。"""
        if not entry:
            return False
        if not ttl or ttl <= 0:
            return True
        return (time.time() - entry.get("time", 0)) < ttl


    @staticmethod
    def video_cache_key(entity_id, limit, include_replies, reply_post_limit=0):
        return f"{entity_id}:{limit}:{include_replies}:{reply_post_limit}"

    @staticmethod
    def reply_cache_key(entity_id, post_id, limit):
        return f"{entity_id}:{post_id}:{limit}"

    def list_videos(
        self,
        dialog_index=None,
        entity_id=None,
        source="dialog",
        limit=100,
        include_replies=False,
        reply_post_limit=50,
        refresh=False,
    ):
        entity, name = self.resolve_requested_entity(source, dialog_index, entity_id)
        if entity is None:
            return {"error": "无效的对话"}, 400

        with self.cache_lock:
            self.current_entity_cache["entity"] = entity
            self.current_entity_cache["name"] = name

        eid = getattr(entity, "id", entity_id)
        if eid is None:
            return {"error": "无法确定对话 ID"}, 400

        current_entity_id = entity_id if entity_id is not None else eid
        cache_key = self.video_cache_key(current_entity_id, limit, include_replies, reply_post_limit)

        with self.cache_lock:
            self.current_entity_cache["entity_id"] = current_entity_id
            cached_videos = self.videos_cache.get(cache_key)

        if cached_videos and not refresh and self._cache_fresh(cached_videos, self.video_cache_ttl):
            return {
                "videos": cached_videos.get("videos", []),
                "posts_with_replies": cached_videos.get("posts_with_replies", []),
                "cached": True,
            }, 200

        async def scan():
            videos = []
            posts_with_replies = []
            async for message in self.client.iter_messages(entity, limit=limit):
                info = self.video_info_for_message(message, current_entity_id)
                if info:
                    videos.append(info)

                if (
                    include_replies
                    and len(posts_with_replies) < reply_post_limit
                    and getattr(message, "replies", None)
                    and message.replies.replies > 0
                ):
                    posts_with_replies.append({
                        "id": message.id,
                        "count": message.replies.replies,
                        "text_excerpt": self.make_excerpt(self.message_text(message), 220),
                    })
            return videos, posts_with_replies

        videos, posts_with_replies = self.run_async(scan)

        with self.cache_lock:
            self.videos_cache[cache_key] = {
                "videos": videos,
                "posts_with_replies": posts_with_replies,
                "time": time.time(),
            }
            while len(self.videos_cache) > self.max_video_cache_size:
                self.videos_cache.pop(next(iter(self.videos_cache)), None)

        return {
            "videos": videos,
            "posts_with_replies": posts_with_replies if include_replies else [],
            "entity_id": current_entity_id,
            "reply_post_limit": reply_post_limit,
            "cached": False,
        }, 200

    def list_replies(self, entity_id, post_id, limit=100, refresh=False):
        if not entity_id or not post_id:
            return {"error": "缺少参数"}, 400

        cache_key = self.reply_cache_key(entity_id, post_id, limit)

        with self.cache_lock:
            cached = self.replies_cache.get(cache_key)

        if cached and not refresh and self._cache_fresh(cached, self.reply_cache_ttl):
            return {"videos": cached.get("videos", []), "cached": True}, 200

        async def scan_one_post_replies():
            with self.cache_lock:
                entity = self.current_entity_cache.get("entity")

            if not entity or getattr(entity, "id", 0) != entity_id:
                entity = await self.client.get_entity(entity_id)

            parent_message = await self.client.get_messages(entity, ids=post_id)
            parent_text = self.message_text(parent_message) if parent_message else ""
            parent_excerpt = self.make_excerpt(parent_text, 260)

            replies_videos = []
            try:
                async for reply in self.client.iter_messages(entity, reply_to=post_id, limit=limit):
                    info = self.video_info_for_message(
                        reply,
                        entity_id,
                        f"评论@帖子{post_id}",
                        {
                            "parent_post_id": post_id,
                            "parent_text": parent_text,
                            "parent_text_excerpt": parent_excerpt,
                        },
                    )
                    if info:
                        replies_videos.append(info)
            except Exception as exc:
                self.log_warning(f"扫描帖子 {post_id} 评论失败: {exc}")
            return replies_videos

        replies_videos = self.run_async(scan_one_post_replies)

        with self.cache_lock:
            self.replies_cache[cache_key] = {"videos": replies_videos, "time": time.time()}
            while len(self.replies_cache) > self.max_reply_cache_size:
                self.replies_cache.pop(next(iter(self.replies_cache)), None)

        return {"videos": replies_videos, "cached": False}, 200

    def search_videos(
        self,
        query,
        dialog_index=None,
        entity_id=None,
        source="dialog",
        limit=200,
        scan_limit=1000,
        include_comments=True,
        comment_post_limit=80,
        comment_limit=100,
    ):
        query = (query or "").strip()
        if not query:
            return {"error": "请输入要搜索的文件名或关键词"}, 400

        entity, name = self.resolve_requested_entity(source, dialog_index, entity_id)
        if entity is None:
            return {"error": "无效的对话"}, 400

        with self.cache_lock:
            self.current_entity_cache["entity"] = entity
            self.current_entity_cache["name"] = name

        eid = getattr(entity, "id", entity_id)
        if eid is None:
            return {"error": "无法确定对话 ID"}, 400

        current_entity_id = entity_id if entity_id is not None else eid
        with self.cache_lock:
            self.current_entity_cache["entity_id"] = current_entity_id

        keyword = query.lower()

        def matches_video(item):
            haystack = " ".join([
                item.get("filename") or "",
                item.get("text") or "",
                item.get("text_excerpt") or "",
                item.get("parent_text_excerpt") or "",
            ]).lower()
            return keyword in haystack

        async def search_channel():
            found = {}
            telegram_hits = 0
            scanned = 0
            comment_posts = []
            comments_scanned = 0
            comment_hits = 0

            async for message in self.client.iter_messages(entity, search=query, limit=limit):
                info = self.video_info_for_message(message, current_entity_id, "频道搜索")
                if info:
                    key = (int(info["entity_id"]), int(info["id"]))
                    found[key] = info
                    telegram_hits += 1

            async for message in self.client.iter_messages(entity, limit=scan_limit):
                scanned += 1
                parent_text = self.message_text(message)
                parent_matched = keyword in parent_text.lower()
                info = self.video_info_for_message(message, current_entity_id, "文件名匹配")
                if info and matches_video(info):
                    key = (int(info["entity_id"]), int(info["id"]))
                    found[key] = info
                if (
                    include_comments
                    and len(comment_posts) < comment_post_limit
                    and getattr(message, "replies", None)
                    and message.replies.replies > 0
                ):
                    comment_posts.append({
                        "id": message.id,
                        "text": parent_text,
                        "excerpt": self.make_excerpt(parent_text, 260),
                        "matched": parent_matched,
                    })

            if include_comments:
                for post in comment_posts:
                    try:
                        async for reply in self.client.iter_messages(entity, reply_to=post["id"], limit=comment_limit):
                            comments_scanned += 1
                            reply_text = self.message_text(reply)
                            reply_matched = keyword in reply_text.lower()
                            info = self.video_info_for_message(
                                reply,
                                current_entity_id,
                                "评论搜索",
                                {
                                    "parent_post_id": post["id"],
                                    "parent_text": post["text"],
                                    "parent_text_excerpt": post["excerpt"],
                                },
                            )
                            if not info:
                                continue
                            if post["matched"] or reply_matched or matches_video(info):
                                if reply_matched or matches_video(info):
                                    comment_hits += 1
                                if post["matched"] and not reply_matched and not matches_video(info):
                                    info["source"] = f"主帖标签匹配@帖子{post['id']}"
                                else:
                                    info["source"] = f"评论匹配@帖子{post['id']}"
                                key = (int(info["entity_id"]), int(info["id"]))
                                found[key] = info
                    except Exception as exc:
                        self.log_warning(f"搜索帖子 {post['id']} 评论失败: {exc}")

            return list(found.values()), telegram_hits, scanned, comments_scanned, comment_hits

        videos, telegram_hits, scanned, comments_scanned, comment_hits = self.run_async(search_channel)
        videos.sort(key=lambda item: item.get("date", ""), reverse=True)
        return {
            "videos": videos,
            "entity_id": current_entity_id,
            "query": query,
            "telegram_hits": telegram_hits,
            "scanned": scanned,
            "comments_scanned": comments_scanned,
            "comment_hits": comment_hits,
            "limit": limit,
            "scan_limit": scan_limit,
            "include_comments": include_comments,
            "comment_post_limit": comment_post_limit,
            "comment_limit": comment_limit,
            "cached": False,
        }, 200

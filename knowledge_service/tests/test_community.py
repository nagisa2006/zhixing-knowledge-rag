import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from knowledge_service.community import add_community_metadata, sync


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _published(post_id, title, content, created_at, updated_at, channel):
    return {
        "id": "post-" + post_id,
        "postId": post_id,
        "title": title,
        "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "publishedAt": created_at,
        "postUpdatedAt": updated_at,
        "channel": channel,
        "authority": "community",
        "status": "published",
    }


def test_sync_paginates_and_publishes_updates_noops_and_disables():
    created_at = "2026-09-01T08:00:00Z"
    source_calls = []
    pages = {
        "": {
            "data": {
                "items": [
                    {
                        "id": "1",
                        "eligible": True,
                        "title": "更新后的标题",
                        "content": "更新后的公开内容",
                        "channel": "互助",
                        "createdAt": created_at,
                        "updatedAt": "2026-09-20T08:00:00Z",
                    },
                    {
                        "id": "2",
                        "eligible": True,
                        "title": "没有变化",
                        "content": "相同的公开内容",
                        "channel": "校园",
                        "createdAt": created_at,
                        "updatedAt": "2026-09-02T08:00:00Z",
                    },
                ],
                "nextCursor": "2",
            }
        },
        "2": {
            "data": {
                "items": [
                    {"id": "3", "eligible": False},
                    {
                        "id": "5",
                        "eligible": True,
                        "title": "新公开帖子",
                        "content": "首次发布的公开内容",
                        "channel": "问答",
                        "createdAt": "2026-09-05T08:00:00Z",
                        "updatedAt": "2026-09-05T08:00:00Z",
                    },
                ],
                "nextCursor": "",
            }
        },
    }

    def source_get(url, *, params, headers, timeout):
        source_calls.append((url, params, headers, timeout))
        return _Response(pages[params["cursor"]])

    documents = [
        _published("1", "旧标题", "旧的公开内容", created_at, "2026-09-01T08:00:00Z", "互助"),
        _published("2", "没有变化", "相同的公开内容", created_at, "2026-09-02T08:00:00Z", "校园"),
        _published("3", "曾经公开", "现在不可见", created_at, "2026-09-03T08:00:00Z", "校园"),
        _published("4", "已经删除", "已从快照消失", created_at, "2026-09-04T08:00:00Z", "校园"),
        {"id": "official-1", "authority": "official", "status": "published"},
    ]
    knowledge_calls = []

    def knowledge_request(path, body=None):
        if path == "/documents" and body is None:
            return {"items": documents}
        knowledge_calls.append((path, body))
        return {"ok": True}

    result = sync(
        "https://backend.internal/api/internal/knowledge/community-posts",
        "source-secret",
        knowledge_request,
        source_get=source_get,
    )

    assert result == {"changed": 4, "publicPosts": 3}
    assert [call[1] for call in source_calls] == [
        {"cursor": "", "limit": 200},
        {"cursor": "2", "limit": 200},
    ]
    assert all(call[2] == {"Authorization": "Bearer source-secret"} for call in source_calls)

    published = [body for path, body in knowledge_calls if path == "/documents"]
    assert [body["id"] for body in published] == ["post-1", "post-5"]
    assert published[0]["content"] == "更新后的公开内容"
    assert all(body["id"] != "post-2" for body in published)
    disabled = [body for path, body in knowledge_calls if path == "/documents/status"]
    assert disabled == [
        {"id": "post-3", "status": "disabled"},
        {"id": "post-4", "status": "disabled"},
    ]
    assert "现在不可见" not in json.dumps(knowledge_calls, ensure_ascii=False)


def test_community_metadata_is_platform_neutral():
    document = {"id": "post-42", "title": "公开帖子", "contentHash": "content-hash"}
    chunks = [{"id": "post-42:content-hash:0"}]
    add_community_metadata(
        document,
        chunks,
        {"postId": "42", "postUpdatedAt": "2026-09-20T08:00:00Z", "channel": "问答"},
    )

    assert document["postId"] == "42"
    assert document["postUpdatedAt"] == "2026-09-20T08:00:00Z"
    assert chunks[0]["postId"] == "42"
    assert chunks[0]["channel"] == "问答"
    serialized = json.dumps({"document": document, "chunks": chunks}, ensure_ascii=False)
    assert "jumpUrl" not in serialized
    assert "/pages/" not in serialized

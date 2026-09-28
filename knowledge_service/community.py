"""Synchronize public community posts from the business backend over HTTP."""
import hashlib
import json
import os

import requests


PAGE_SIZE = 200


def _content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def add_community_metadata(document, chunks, body):
    """Attach platform-neutral post provenance to a community document."""
    document.update({
        "postId": str(body["postId"]),
        "postUpdatedAt": body["postUpdatedAt"],
        "channel": body.get("channel", ""),
    })
    for chunk in chunks:
        chunk.update({
            "type": "post",
            "postId": str(body["postId"]),
            "postContentHash": document["contentHash"],
            "postTitle": document["title"],
            "channel": body.get("channel", ""),
        })


def fetch_rows(source_url, source_api_key, *, get=None, page_size=PAGE_SIZE):
    """Fetch a complete visibility-filtered snapshot before changing the index."""
    get = get or requests.get
    cursor = ""
    seen_cursors = set()
    rows = []
    while True:
        response = get(
            source_url,
            params={"cursor": cursor, "limit": page_size},
            headers={"Authorization": "Bearer " + source_api_key},
            timeout=180,
        )
        response.raise_for_status()
        page = response.json()["data"]
        rows.extend(page["items"])
        next_cursor = page.get("nextCursor", "")
        if not next_cursor:
            return rows
        if next_cursor == cursor or next_cursor in seen_cursors:
            raise ValueError("Community source returned a repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def sync(source_url, source_api_key, request, *, source_get=None):
    """Publish the current eligible set and disable documents absent from it."""
    rows = fetch_rows(source_url, source_api_key, get=source_get)
    previous = {
        document["id"]: document
        for document in request("/documents")["items"]
        if document.get("authority") == "community"
    }
    active = set()
    changed = 0
    for row in rows:
        post_id = str(row["id"])
        document_id = "post-" + post_id
        if not row["eligible"]:
            active.discard(document_id)
            continue
        active.add(document_id)
        old = previous.get(document_id)
        unchanged = (
            old
            and old.get("status") == "published"
            and old.get("postId") == post_id
            and old.get("contentHash") == _content_hash(row["content"])
            and old.get("title") == row["title"]
            and old.get("publishedAt") == row["createdAt"]
            and old.get("postUpdatedAt") == row["updatedAt"]
            and old.get("channel") == row["channel"]
        )
        if unchanged:
            continue
        request(
            "/documents",
            {
                "id": document_id,
                "postId": post_id,
                "title": row["title"],
                "content": row["content"],
                "sourceId": "public-forum",
                "sourceUrl": "",
                "authority": "community",
                "publishedAt": row["createdAt"],
                "postUpdatedAt": row["updatedAt"],
                "channel": row["channel"],
            },
        )
        changed += 1
    for document_id, document in previous.items():
        if document_id not in active and document.get("status") == "published":
            request("/documents/status", {"id": document_id, "status": "disabled"})
            changed += 1
    return {"changed": changed, "publicPosts": len(active)}


def main():
    source_url = os.environ["KNOWLEDGE_COMMUNITY_SOURCE_URL"]
    source_api_key = os.environ["KNOWLEDGE_COMMUNITY_SOURCE_API_KEY"]
    knowledge_url = os.environ["KNOWLEDGE_URL"]
    knowledge_api_key = os.environ["KNOWLEDGE_API_KEY"]
    if not source_api_key or not knowledge_api_key:
        raise ValueError("Community source and knowledge API keys must be configured")

    def request(path, body=None):
        response = requests.request(
            "POST" if body is not None else "GET",
            knowledge_url + path,
            headers={"Authorization": "Bearer " + knowledge_api_key},
            json=body,
            timeout=180,
        )
        response.raise_for_status()
        return response.json()

    print(json.dumps(sync(source_url, source_api_key, request)))


if __name__ == "__main__":
    main()

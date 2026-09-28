"""Private knowledge API. Start with python -m knowledge_service.server."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from .community import add_community_metadata
from .engine import HybridEngine
from .ingest import make_document, fetch_document
from .models import Models
from .store import KnowledgeStore


LOG=logging.getLogger("xiaodian.knowledge")


def serve():
    store=KnowledgeStore(os.environ["KNOWLEDGE_DB"])
    engine=HybridEngine(store,Models())
    api_key=os.environ["KNOWLEDGE_API_KEY"]
    if not api_key:
        raise ValueError("KNOWLEDGE_API_KEY must be configured")

    class Handler(BaseHTTPRequestHandler):
        def send(self,status,payload):
            raw=json.dumps(payload,ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def auth(self):
            if not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+api_key):
                self.send(401,{"error":"Unauthorized"})
                return False
            return True

        def do_GET(self):
            path=urlsplit(self.path).path
            if path=="/health":
                self.send(200,{"ready":engine.status()["ready"]})
                return
            if not self.auth():
                return
            if path=="/status":
                self.send(200,engine.status())
            elif path=="/documents":
                self.send(200,{"items":store.documents()})
            elif path.startswith("/documents/"):
                parts=path.split("/")
                if len(parts) == 4 and parts[-1] == "content":
                    doc=store.get_document_content(parts[-2])
                else:
                    doc=store.get_document(parts[-1])
                self.send(200 if doc else 404,doc or {"error":"Document not found"})
            elif path.startswith("/traces/"):
                trace=store.get_trace(path.split("/")[-1])
                self.send(200 if trace else 404,trace or {"error":"Trace not found"})
            else:
                self.send(404,{"error":"Not found"})

        def do_POST(self):
            if not self.auth():
                return
            length=int(self.headers.get("Content-Length","0"))
            if length>2*1024*1024:
                self.send(413,{"error":"Request too large"})
                return
            try:
                body=json.loads(self.rfile.read(length))
                path=urlsplit(self.path).path
                if path=="/search":
                    result=engine.search(body["query"],queries=body.get("queries"),limit=int(body.get("limit",8)),
                        include_community=body.get("includeCommunity",False),filters=body.get("filters"),trace_id=body.get("traceId"))
                elif path=="/reindex":
                    result=engine.build_index()
                elif path=="/ingest":
                    doc,chunks=fetch_document(body["url"],Path(os.environ["KNOWLEDGE_RAW_DIR"]))
                    result={"document":doc,"index":engine.publish_document(doc,chunks)}
                elif path=="/documents":
                    if body.get("supersedesId") and store.get_document(body["supersedesId"]) is None:
                        raise ValueError("被替代的文档不存在")
                    if body.get("sourceUrl") and not body.get("content"):
                        fetched,_=fetch_document(body["sourceUrl"],Path(os.environ["KNOWLEDGE_RAW_DIR"]))
                        body={**fetched,**body,"content":fetched["content"],
                            "title":body.get("title") or fetched["title"],"authority":"official",
                            "publishedAt":fetched["publishedAt"],
                            "rawObjectKey":fetched.get("rawObjectKey", "")}
                    doc,chunks=make_document(body["title"],body["content"],body["sourceUrl"],
                        authority=body.get("authority","curated"),published_at=body.get("publishedAt"),
                        source_id=body.get("sourceId"),doc_id=body.get("id"))
                    for field in ("effectiveFrom","effectiveTo","supersedesId","campuses","educationLevels"):
                        if field in body:
                            doc[field]=body[field]
                            for chunk in chunks:
                                chunk[field]=body[field]
                    if body.get("rawObjectKey"):
                        doc["rawObjectKey"] = str(body["rawObjectKey"])
                    if (not doc["title"].strip() and doc["authority"]!="community") or not doc["content"].strip():
                        raise ValueError("标题和正文不能为空")
                    doc["chunkCount"]=len(chunks)
                    if doc["authority"]=="community":
                        add_community_metadata(doc,chunks,body)
                    result={"document":doc,"index":engine.publish_document(doc,chunks)}
                elif path=="/documents/status":
                    if body["status"] not in {"published","disabled","archived"}:
                        raise ValueError("Invalid document status")
                    engine.set_document_status(body["id"],body["status"])
                    result={"ok":True}
                elif path=="/traces":
                    store.trace(body["traceId"],body)
                    result={"ok":True}
                else:
                    self.send(404,{"error":"Not found"})
                    return
                self.send(200,result)
            except (ValueError,KeyError) as error:
                self.send(400,{"error":str(error)})
            except Exception:
                LOG.exception("knowledge request failed")
                self.send(502,{"error":"知识服务处理失败，请通过服务日志定位"})

    logging.basicConfig(level=logging.INFO)
    host=os.environ.get("KNOWLEDGE_HOST","127.0.0.1")
    port=int(os.environ.get("KNOWLEDGE_PORT","8091"))
    LOG.info("knowledge service listening on %s:%s",host,port)
    ThreadingHTTPServer((host,port),Handler).serve_forever()


if __name__=="__main__":
    serve()

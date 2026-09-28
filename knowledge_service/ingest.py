"""Fetch official documents, extract body text, preserve provenance and chunk structure."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from email.message import Message
import json
from pathlib import Path
import re
import uuid
import hashlib
from urllib.parse import urlsplit, unquote

import requests
import trafilatura
from pypdf import PdfReader

from .store import KnowledgeStore, content_hash, utcnow
from .attachments import office_text, ocr_pdf


def official_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "xidian.edu.cn" or host.endswith(".xidian.edu.cn") or host == "moe.gov.cn" or host.endswith(".moe.gov.cn")


def split_text(text: str, size=750, overlap=80) -> list[str]:
    """Keep paragraphs intact when possible; even a single long paragraph is bounded."""
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    output, current = [], ""
    for para in paragraphs:
        while len(para) > size:
            if current:
                output.append(current)
                current = ""
            cut = max(para.rfind(mark, size//2, size) for mark in "。；！？\n")
            cut = cut+1 if cut >= 0 else size
            output.append(para[:cut])
            para = para[max(1,cut-overlap):]
        if current and len(current)+len(para)+1 > size:
            output.append(current)
            current = current[-overlap:] + "\n" + para if len(para)+overlap+1 <= size else para
        else:
            current = (current+"\n"+para).strip()
    if current:
        output.append(current)
    return list(dict.fromkeys(output))


def make_document(title: str, text: str, url: str, *, published_at=None,
                  authority="official", source_id=None, doc_id=None, pages=None) -> tuple[dict,list[dict]]:
    document_id = doc_id or ("doc-"+content_hash(url)[:24] if url else "doc-"+uuid.uuid4().hex)
    revision = content_hash(text)
    fetched = utcnow()
    doc = {
        "id":document_id, "sourceId":source_id or "source-"+content_hash(urlsplit(url).netloc)[:16],
        "title":title, "content":text, "sourceUrl":url, "authority":authority,
        "publishedAt":published_at, "fetchedAt":fetched, "createdAt":fetched,
        "updatedAt":fetched, "contentHash":revision, "status":"published",
        "parserVersion":"xiaodian-body-1", "effectiveFrom":None, "effectiveTo":None,
        "supersedesId":None, "campuses":[], "educationLevels":[],
    }
    chunks=[]
    for name, aliases in {"changan":("长安校区","南校区"),"yanta":("雁塔校区","北校区")}.items():
        if any(alias in title for alias in aliases):
            doc["campuses"].append(name)
    for level, alias in {"undergraduate":"本科", "graduate":"研究生"}.items():
        if alias in title:
            doc["educationLevels"].append(level)
    source_pages = pages if pages is not None else [(None,text)]
    for page_number, body in source_pages:
        for content in split_text(body):
            ordinal=len(chunks)
            chunk_id=f"{document_id}:{revision[:12]}:{ordinal}"
            chunks.append({
                "id":chunk_id, "key":"knowledge:"+chunk_id, "type":"knowledge",
                "documentId":document_id, "sourceId":doc["sourceId"],
                "ordinal":ordinal, "title":title, "content":content,
                "sourceUrl":url, "sourceName":urlsplit(url).netloc,
                "authority":authority, "publishedAt":published_at, "createdAt":fetched,
                "updatedAt":fetched, "revision":revision, "pageNumber":page_number,
                "contentHash":content_hash(content), "status":"published",
                "campuses":doc["campuses"], "educationLevels":doc["educationLevels"],
                "searchText":f"{title}\n原文日期：{published_at or '未标明'}\n{content}",
            })
    return doc,chunks


def fetch_document(url: str, raw_dir: Path) -> tuple[dict,list[dict]]:
    if not official_url(url):
        raise ValueError("只摄取配置范围内的官方来源")
    response=requests.get(url,timeout=(10,30),headers={"User-Agent":"ZhixingKnowledge/2.0 (+https://zhixing.xidian.edu.cn)"})
    response.raise_for_status()
    if not official_url(response.url):
        raise ValueError("原文重定向到来源范围以外")
    raw_dir.mkdir(parents=True,exist_ok=True)
    raw_path=raw_dir/(hashlib.sha256(response.content).hexdigest()+".source")
    raw_path.write_bytes(response.content)
    content_type=response.headers.get("Content-Type","")
    disposition=Message()
    disposition['Content-Disposition']=response.headers.get('Content-Disposition','')
    filename=unquote(disposition.get_filename() or Path(urlsplit(response.url).path).name)
    suffix=Path(filename).suffix.lower()
    pages=None
    if "pdf" in content_type or suffix==".pdf":
        reader=PdfReader(BytesIO(response.content))
        pages=[(i+1,(page.extract_text(extraction_mode="layout") or "").strip()) for i,page in enumerate(reader.pages)]
        scanned=[number-1 for number,body in pages if len(body)<80]
        if scanned:
            recognized=ocr_pdf(response.content,scanned)
            pages=[(number,recognized.get(number-1,body)) for number,body in pages]
        text="\n\n".join(body for _,body in pages)
        if len(text.strip()) < 80:
            raise ValueError("PDF 正文与 OCR 均无可用文字，未发布空白资料")
        title=filename if suffix=='.pdf' else str((reader.metadata or {}).get("/Title") or 'PDF 文档')
        published_at=None
    elif suffix in {".docx",".xlsx",".doc",".xls"}:
        text=office_text(response.content,suffix)
        title=filename
        published_at=None
    else:
        from .html_body import parse_html
        title,text,published_at,pages=parse_html(response.content,response.url)
    doc,chunks=make_document(title,text,response.url,published_at=published_at,pages=pages)
    doc["rawObjectKey"]=str(raw_path.resolve())
    doc["chunkCount"]=len(chunks)
    return doc,chunks


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--db",required=True)
    parser.add_argument("--urls",required=True)
    parser.add_argument("--raw-dir",required=True)
    parser.add_argument("--workers",type=int,default=8)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--report",required=True)
    args=parser.parse_args()
    urls=list(dict.fromkeys(json.loads(Path(args.urls).read_text(encoding="utf-8"))))
    if args.limit:
        urls=urls[:args.limit]
    store=KnowledgeStore(args.db)
    report={"startedAt":utcnow(),"requested":len(urls),"published":[],"failed":[]}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending={pool.submit(fetch_document,url,Path(args.raw_dir)):url for url in urls}
        for future in as_completed(pending):
            url=pending[future]
            try:
                doc,chunks=future.result()
                store.put_document(doc,chunks)
                store.job(url,"published")
                report["published"].append({"id":doc["id"],"url":url,"title":doc["title"],"chunks":len(chunks),"publishedAt":doc["publishedAt"]})
            except Exception as error:
                detail=str(error)
                store.job(url,"failed",detail)
                report["failed"].append({"url":url,"error":detail})
            done=len(report["published"])+len(report["failed"])
            if done%25==0:
                print(json.dumps({"done":done,"published":len(report["published"]),"failed":len(report["failed"])}),flush=True)
    report["completedAt"]=utcnow()
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"published":len(report["published"]),"failed":len(report["failed"])},ensure_ascii=False))


if __name__=="__main__":
    main()

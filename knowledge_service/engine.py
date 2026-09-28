"""Versioned Qdrant dense retrieval + Chinese BM25 + neural reranking."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
import os
import threading
import time
import uuid

from qdrant_client import QdrantClient, models as qm

from .lexical import BM25, reciprocal_rank_fusion
from .store import KnowledgeStore, content_hash, utcnow


def narrow_candidates(rows,*,today,campus=None,level=None,limit=12):
    """Dedup, drop out-of-scope rows, then keep the top `limit` in input order.

    `rows` arrives in RRF order and the slice has to stay last.  Reranking a pair
    costs about a hundred times what recalling it does, so only `limit` pairs can
    be afforded -- but cutting before the filters would let a withdrawn, superseded
    or out-of-scope chunk occupy one of those slots and silently starve recall.
    Returns the filtered list and the slice, so callers can trace both counts.
    """
    distinct={}
    for row in rows:
        distinct.setdefault(row["contentHash"],row)
    kept=[row for row in distinct.values()
        if (not campus or not row.get("campuses") or campus in row["campuses"])
        and (not level or not row.get("educationLevels") or level in row["educationLevels"])
        and (not row.get("effectiveFrom") or row["effectiveFrom"]<=today)
        and (not row.get("effectiveTo") or row["effectiveTo"]>=today)]
    return kept,kept[:limit]


class HybridEngine:
    def __init__(self,store:KnowledgeStore,models):
        self.store,self.models=store,models
        self.qdrant=QdrantClient(url=os.environ["KNOWLEDGE_QDRANT_URL"],
            api_key=os.environ.get("KNOWLEDGE_QDRANT_API_KEY") or None,timeout=30)
        self.lock=threading.RLock()
        self.index_write_lock=threading.Lock()
        # Required, not defaulted: a rerank threshold is only meaningful for the
        # model it was calibrated against, and inheriting another model's cutoff
        # either drops good evidence or admits unrelated passages -- neither of
        # which shows up as an error.  Fail at startup instead.
        if "KNOWLEDGE_RERANK_THRESHOLD" not in os.environ:
            raise RuntimeError("必须设置 KNOWLEDGE_RERANK_THRESHOLD，且需针对当前重排模型重新校准")
        self.rerank_threshold=float(os.environ["KNOWLEDGE_RERANK_THRESHOLD"])
        self._load_lexical()

    def _load_lexical(self):
        self.lexical=BM25(self.store.chunks())

    def build_index(self):
        with self.index_write_lock:
            return self._build_index()

    def _build_index(self):
        rows=self.store.chunks()
        if not rows:
            raise ValueError("知识库没有已发布片段，不能建立空索引")
        version="zhixing_v2_"+uuid.uuid4().hex[:16]
        self.qdrant.create_collection(version,
            vectors_config=qm.VectorParams(size=self.models.dimension,distance=qm.Distance.COSINE))
        for field in ("authority","documentId"):
            self.qdrant.create_payload_index(version,field_name=field,field_schema=qm.PayloadSchemaType.KEYWORD)
        for start in range(0,len(rows),32):
            batch=rows[start:start+32]
            vectors=self.models.encode([row["searchText"] for row in batch])
            points=[qm.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL,row["id"])),vector=vector,
                payload={"chunkId":row["id"],"documentId":row["documentId"],"authority":row["authority"]})
                for row,vector in zip(batch,vectors)]
            self.qdrant.upsert(version,points=points,wait=True)
            if start%320==0:
                print(f"index {version}: {min(start+32,len(rows))}/{len(rows)}",flush=True)
        count=self.qdrant.count(version,exact=True).count
        if count!=len(rows):
            raise RuntimeError(f"向量数量不符：{count}/{len(rows)}")
        manifest={"collection":version,"chunkCount":count,"createdAt":utcnow(),
            "embeddingModel":self.models.embedding_name,"dimension":self.models.dimension,
            "corpusHash":content_hash("\n".join(sorted(row["id"] for row in rows)))}
        with self.lock:
            self.store.set_setting("activeIndex",manifest)
            self._load_lexical()
        return manifest

    def publish_document(self, doc, chunks):
        with self.index_write_lock:
            active = self.store.setting("activeIndex")
            if active:
                vectors = self.models.encode([row["searchText"] for row in chunks])
                self.qdrant.upsert(active["collection"], points=[
                    qm.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, row["id"])), vector=vector,
                        payload={"chunkId":row["id"], "documentId":row["documentId"], "authority":row["authority"]})
                    for row,vector in zip(chunks,vectors)
                ], wait=True)
            with self.lock:
                self.store.put_document(doc, chunks)
                self._load_lexical()
                if active:
                    self._record_corpus_update(active)
            return active

    def _record_corpus_update(self, active):
        active["updatedAt"] = utcnow()
        active["chunkCount"] = len(self.lexical.rows)
        active["corpusHash"] = content_hash("\n".join(sorted(self.lexical.rows)))
        self.store.set_setting("activeIndex", active)

    def set_document_status(self, doc_id, status):
        with self.index_write_lock, self.lock:
            self.store.set_status(doc_id,status)
            self._load_lexical()
            active=self.store.setting('activeIndex')
            if active:
                self._record_corpus_update(active)

    def status(self):
        active=self.store.setting("activeIndex")
        return {"ready":active is not None,"index":active,
            "documentCount":len(self.store.documents(published_only=True)),
            "knowledgeCount":sum(r.get("type")!="post" for r in self.lexical.rows.values()),
            "postCount":sum(r.get("type")=="post" for r in self.lexical.rows.values()),
            "embeddingModel":self.models.embedding_name,"rerankModel":self.models.reranker_name}

    def search(self,query:str,*,queries=None,limit=8,include_community=False,filters=None,trace_id=None):
        started=time.perf_counter()
        trace_id=trace_id or uuid.uuid4().hex
        queries=list(dict.fromkeys(queries or [query]))[:3]
        filters=filters or {}
        with self.lock:
            active=self.store.setting("activeIndex")
            lexical=self.lexical
        if active is None:
            raise RuntimeError("知识库尚未建立混合索引")
        if active["embeddingModel"]!=self.models.embedding_name or active["dimension"]!=self.models.dimension:
            raise RuntimeError("向量模型与已发布索引不一致，需要重建索引")
        authorities=["official","verified","curated"]
        if include_community:
            authorities.append("community")
        allowed={key for key,row in lexical.rows.items() if row["authority"] in authorities}
        encode_started=time.perf_counter()
        vector_queries=self.models.encode(queries)
        encode_ms=round((time.perf_counter()-encode_started)*1000)
        rankings=[]
        stages=[]
        for text,vector in zip(queries,vector_queries):
            dense=self.qdrant.query_points(active["collection"],query=vector,limit=30,
                query_filter=qm.Filter(must=[qm.FieldCondition(key="authority",match=qm.MatchAny(any=authorities))]),
                with_payload=True).points
            dense_rows=[(p.payload["chunkId"],float(p.score)) for p in dense]
            sparse_rows=lexical.search(text,limit=30,allowed=allowed)
            rankings.extend([dense_rows,sparse_rows])
            stages.append({"query":text,"dense":dense_rows,"bm25":sparse_rows})
        fused=reciprocal_rank_fusion(rankings,limit=60)
        # The current relational state decides visibility, even if old vectors remain.
        # get_chunks returns rows in the order of the ids it is given, so the RRF
        # ranking survives into narrow_candidates, which relies on it.
        fetched=self.store.get_chunks([key for key,_ in fused])
        filtered,candidates=narrow_candidates(fetched,
            today=datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
            campus=filters.get("campus"),level=filters.get("educationLevel"),
            limit=int(os.environ.get("KNOWLEDGE_RERANK_CANDIDATES","12")))
        filtered_count=len(filtered)
        rerank_stats={}
        rerank_started=time.perf_counter()
        ranked=self.models.rerank(query,candidates,stats=rerank_stats)
        threshold=self.rerank_threshold
        selected=[]
        counts=Counter()
        for row in ranked:
            if row["rerankScore"]<threshold:
                continue
            if counts[row["documentId"]]>=3:
                continue
            selected.append(row)
            counts[row["documentId"]]+=1
            if len(selected)>=limit:
                break
        results=[]
        seen=set()
        chars=0
        context_budget=int(os.environ.get("KNOWLEDGE_CONTEXT_CHAR_BUDGET","16000"))
        # Each matched passage stays intact; neighbors supply missing adjacent clauses.
        for row in selected:
            for part in [row,*self.store.neighbors(row["documentId"],row["ordinal"])]:
                if part["id"] in seen or chars+len(part["content"])>context_budget:
                    continue
                seen.add(part["id"])
                chars+=len(part["content"])
                results.append({**part,"score":row["rerankScore"],
                    "retrievalRole":"match" if part["id"]==row["id"] else "neighbor",
                    "summary":part["content"][:260]})
        trace={"traceId":trace_id,"indexVersion":active["collection"],"retrievalMode":"dense_bm25_rrf_rerank",
            "corpusHash":active['corpusHash'],
            "query":query,"queries":queries,"filters":filters,"stages":stages,"fused":fused,
            "reranked":[{"id":r["id"],"score":r["rerankScore"]} for r in ranked],
            "contextIds":[r["id"] for r in results],
            # Counts at every narrowing step: a drop from fused to filtered points at
            # visibility or scope, a drop from filtered to reranked is this host's
            # latency budget, and truncatedPairs says whether the budget cost evidence.
            "queryCount":len(queries),"fusedCount":len(fused),"filteredCount":filtered_count,
            "rerankedCount":len(ranked),"selectedCount":len(selected),
            **rerank_stats,
            "rerankThreshold":threshold,
            "encodeMs":encode_ms,
            "rerankMs":round((time.perf_counter()-rerank_started)*1000),
            "retrievalMs":round((time.perf_counter()-started)*1000)}
        self.store.trace(trace_id,trace)
        return {"items":results,"trace":trace}

"""Local BGE inference; model paths/revisions are deployment-owned."""
import os
import threading
import torch
from sentence_transformers import SentenceTransformer, CrossEncoder


class Models:
    def __init__(self):
        self.device=os.environ["KNOWLEDGE_MODEL_DEVICE"]
        self.embedding_name=os.environ["KNOWLEDGE_EMBEDDING_REVISION"]
        self.reranker_name=os.environ["KNOWLEDGE_RERANK_REVISION"]
        self.lock=threading.Lock()
        # Rerank cost is linear in batch*seq, so these three decide whether a query
        # answers in a second or in half a minute on a CPU-only host.  Measured
        # defaults for 4 cores: see docs/ai/生产主机算力实测.md.
        self.rerank_max_length=int(os.environ.get("KNOWLEDGE_RERANK_MAX_LENGTH","256"))
        self.rerank_batch_size=int(os.environ.get("KNOWLEDGE_RERANK_BATCH_SIZE","4"))
        if not self.device.startswith("cuda"):
            # Left unset, torch takes every core and starves the business backend
            # that shares this host.
            torch.set_num_threads(int(os.environ.get("KNOWLEDGE_TORCH_THREADS","4")))
        dtype=torch.float16 if self.device.startswith("cuda") else torch.float32
        self.embedding=SentenceTransformer(os.environ["KNOWLEDGE_EMBEDDING_MODEL"],device=self.device,
            local_files_only=True,model_kwargs={"torch_dtype":dtype})
        # Embedding length is not configurable: changing it invalidates the vectors
        # already in Qdrant and forces a full reindex.
        self.embedding.max_seq_length=1024
        self.reranker=CrossEncoder(os.environ["KNOWLEDGE_RERANK_MODEL"],device=self.device,
            max_length=self.rerank_max_length,
            local_files_only=True,model_kwargs={"torch_dtype":dtype})
        if self.device.startswith("cuda"):
            self.embedding.half()
            self.reranker.model.half()
            torch.cuda.empty_cache()
        self.dimension=self.embedding.get_sentence_embedding_dimension()

    def encode(self,texts:list[str]):
        with self.lock,torch.inference_mode():
            return self.embedding.encode(texts,batch_size=8,normalize_embeddings=True,show_progress_bar=False).tolist()

    def rerank(self,query:str,rows:list[dict],stats:dict|None=None):
        if not rows:
            return []
        # max_length caps the question and the passage together, so the passage is
        # what gets cut.  This only shortens what the reranker scores; the answer
        # context keeps the full content and is bounded separately by
        # KNOWLEDGE_CONTEXT_CHAR_BUDGET.
        pairs=[(query,row["title"]+"\n"+row["content"]) for row in rows]
        if stats is not None:
            # The model truncates silently, so the ratio has to be measured here or
            # a config that drops half of every passage looks perfectly healthy.
            encoded=self.reranker.tokenizer([p[0] for p in pairs],[p[1] for p in pairs],
                truncation=False,verbose=False)["input_ids"]
            lengths=[len(ids) for ids in encoded]
            stats.update({"rerankMaxLength":self.rerank_max_length,"rerankBatchSize":self.rerank_batch_size,
                "pairTokensMax":max(lengths),"pairTokensMean":round(sum(lengths)/len(lengths)),
                "truncatedPairs":sum(1 for n in lengths if n>self.rerank_max_length)})
        with self.lock,torch.inference_mode():
            scores=self.reranker.predict(pairs,batch_size=self.rerank_batch_size,
                show_progress_bar=False,activation_fn=torch.nn.Sigmoid())
        return sorted([{**row,"rerankScore":float(score)} for row,score in zip(rows,scores)],key=lambda row:row["rerankScore"],reverse=True)

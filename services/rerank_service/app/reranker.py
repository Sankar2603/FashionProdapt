"""
Cross-encoder reranker (BAAI/bge-reranker-v2-m3), scored directly with
Hugging Face transformers.

A cross-encoder reads the query and the product text TOGETHER and outputs one
relevance score. More accurate than comparing separately-built vectors, but too
slow to run over the whole catalogue, so it only re-scores retrieval's top ~20.

Why not FlagEmbedding's FlagReranker here: on every call it runs an extra
trial batch to pick a batch size and re-tokenizes inputs through a
compatibility layer. Calling the model directly gives the same scores with
less work.

Settings (env, all optional):
    RERANKER_MODEL       default BAAI/bge-reranker-v2-m3
    RERANKER_DEVICE      default cpu   ("cuda" on a GPU)
    RERANKER_MAX_LENGTH  default 256   (tokens per query+product pair)
    RERANKER_BATCH_SIZE  default 16
"""

import os

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


class Reranker:
    def __init__(self):
        self.model_name = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        self.device = os.getenv("RERANKER_DEVICE", "cpu")
        self.max_length = int(os.getenv("RERANKER_MAX_LENGTH", "256"))
        self.batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "16"))

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
        if self.device.startswith("cuda"):
            self.model.half()  # fp16: faster and half the memory on a GPU
        self.model.to(self.device)
        self.model.eval()

    @torch.inference_mode()
    def score(self, query: str, texts: list[str]) -> list[float]:
        """Relevance of each text to the query, 0..1 (sigmoid of the model's logit)."""
        if not texts:
            return []

        # Batch texts of similar length together: less padding = less wasted work.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
        scores = [0.0] * len(texts)

        for start in range(0, len(order), self.batch_size):
            batch_ids = order[start:start + self.batch_size]
            inputs = self.tokenizer(
                [query] * len(batch_ids),
                [texts[i] for i in batch_ids],
                padding=True,
                truncation="longest_first",   # the long product text gets cut, not the query
                max_length=self.max_length,
                return_tensors="pt",
            ).to(self.device)
            logits = self.model(**inputs).logits.view(-1).float()
            for i, p in zip(batch_ids, torch.sigmoid(logits).tolist()):
                scores[i] = p
        return scores
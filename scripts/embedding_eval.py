r"""Evaluate the configured embedding model on a 20-pair Korean/English set.

This is a manual check, not part of the offline test suite. It downloads the
model once to the Hugging Face cache.

    .\.venv\Scripts\python.exe scripts\embedding_eval.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from model2vec import StaticModel

PAIRS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval_pairs.json"


def main() -> None:
    payload = json.loads(PAIRS.read_text(encoding="utf-8"))
    pairs = payload["pairs"]
    model = StaticModel.from_pretrained(payload["model"])
    docs = [item["doc"] for item in pairs]
    queries = [item["query"] for item in pairs]

    doc_vectors = np.asarray(model.encode(docs, normalize=True))
    query_vectors = np.asarray(model.encode(queries, normalize=True))
    scores = query_vectors @ doc_vectors.T

    ranks: list[int] = []
    for index, row in enumerate(scores):
        order = np.argsort(-row)
        ranks.append(int(np.where(order == index)[0][0]) + 1)

    recall_at_1 = sum(rank <= 1 for rank in ranks) / len(ranks)
    recall_at_3 = sum(rank <= 3 for rank in ranks) / len(ranks)
    mrr = sum(1.0 / rank for rank in ranks) / len(ranks)
    by_lang = {}
    for lang in ("ko", "en"):
        subset = [rank for rank, item in zip(ranks, pairs) if item["lang"] == lang]
        by_lang[lang] = sum(rank <= 3 for rank in subset) / len(subset)

    print(f"model={payload['model']} pairs={len(pairs)}")
    print(f"recall@1={recall_at_1:.3f} recall@3={recall_at_3:.3f} mrr={mrr:.3f}")
    print(f"recall@3 ko={by_lang['ko']:.3f} en={by_lang['en']:.3f}")


if __name__ == "__main__":
    main()

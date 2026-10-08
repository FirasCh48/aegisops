"""Index lexical BM25.

Complément du dense, pas concurrent. Les deux échouent sur des choses
différentes : le dense manque les identifiants rares (noms de métriques,
d'événements, de versions), le lexical manque les reformulations.

Cas observé en J3 : la requête de `memory_leak` contient
`process memory MB: 86 -> 393`, et le terme `process_memory_rss_mb`
n'apparaît que dans un seul postmortem du corpus. Le dense a renvoyé
`internal_error` sur une proximité sémantique vague ; BM25 devrait
trouver le bon document sur ce seul terme.
"""
from __future__ import annotations

import json
import pickle
import re
import time
from pathlib import Path

import bm25s
import Stemmer

from aegisops.rag.dense import Hit

ROOT = Path(__file__).resolve().parents[3]
CHUNKS = ROOT / "data" / "corpus" / "chunks.jsonl"
INDEX_DIR = ROOT / "data" / "corpus" / "bm25_index"

_stemmer = Stemmer.Stemmer("english")

# Les identifiants techniques portent l'information la plus discriminante
# du corpus : db_pool_timeout, process_memory_rss_mb, StockLedgerCorruption.
# Le tokenizer par défaut les couperait sur les underscores et les points,
# diluant précisément ce qui les rend uniques. On les préserve entiers,
# tout en gardant aussi leurs composants pour les requêtes partielles.
IDENTIFIER_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]*(?:[_.][a-zA-Z0-9]+)+")


def tokenize(text: str) -> list[str]:
    identifiers = IDENTIFIER_RE.findall(text)
    # Le texte restant passe par le tokenizer standard : minuscules,
    # découpage sur la ponctuation, stemming.
    words = re.findall(r"[a-zA-Z]{2,}", text.lower())
    stemmed = _stemmer.stemWords(words)
    return stemmed + [i.lower() for i in identifiers]


class SparseIndex:
    def __init__(self) -> None:
        self.retriever: bm25s.BM25 | None = None
        self.chunks: list[dict] = []

    def build(self, chunks_path: Path = CHUNKS) -> int:
        self.chunks = [json.loads(l) for l in chunks_path.open(encoding="utf-8")]
        if not self.chunks:
            raise RuntimeError(f"aucun chunk dans {chunks_path}")

        print(f"[bm25] {len(self.chunks)} chunks à indexer")
        start = time.perf_counter()

        corpus_tokens = [tokenize(c["text"]) for c in self.chunks]
        self.retriever = bm25s.BM25()
        self.retriever.index(corpus_tokens)

        elapsed = time.perf_counter() - start
        print(f"  indexation : {elapsed:.2f}s")

        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        self.retriever.save(str(INDEX_DIR))
        (INDEX_DIR / "chunks.pkl").write_bytes(pickle.dumps(self.chunks))
        print(f"  écrit : {INDEX_DIR}")
        return len(self.chunks)

    def load(self) -> None:
        self.retriever = bm25s.BM25.load(str(INDEX_DIR))
        self.chunks = pickle.loads((INDEX_DIR / "chunks.pkl").read_bytes())

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        if self.retriever is None:
            self.load()

        results, scores = self.retriever.retrieve(
            [tokenize(query)], k=min(limit, len(self.chunks))
        )

        hits = []
        for idx, score in zip(results[0], scores[0]):
            c = self.chunks[int(idx)]
            hits.append(Hit(
                chunk_id=c["chunk_id"],
                doc_id=c["doc_id"],
                score=float(score),
                text=c["text"],
                origin=c["origin"],
                scenario=c.get("scenario", ""),
                section=c.get("section", ""),
                title=c.get("title", ""),
            ))
        return hits


def main() -> None:
    index = SparseIndex()
    index.build()

    print("\n--- test ---")
    queries = [
        "process memory MB grew from 86 to 393 with no errors",
        "db_pool_timeout events and pool usage reaching 1.0",
        "dependency_error with inventory stock total unchanged",
    ]
    for q in queries:
        print(f"\n  « {q} »")
        for hit in index.search(q, limit=3):
            label = hit.scenario or hit.title[:38]
            mark = "*" if hit.origin == "sandbox" else " "
            print(f"   {mark} {hit.score:.3f}  {hit.chunk_id:<30} {label}")


if __name__ == "__main__":
    main()
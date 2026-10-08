"""Index dense : embeddings bge-small-en-v1.5 dans Qdrant.

Le modèle est monolingue anglais, ce qui a motivé la traduction des
postmortems du bac à sable en J1 : du texte français y serait mal
représenté, et la recherche échouerait précisément sur les documents qui
comptent le plus.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

ROOT = Path(__file__).resolve().parents[3]
CHUNKS = ROOT / "data" / "corpus" / "chunks.jsonl"

MODEL_NAME = "BAAI/bge-small-en-v1.5"
COLLECTION = "aegisops_corpus"
VECTOR_SIZE = 384

# bge demande ce préfixe sur les requêtes, pas sur les documents. Il a été
# utilisé pendant l'entraînement pour distinguer les deux rôles ; l'omettre
# dégrade la similarité de quelques points.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    score: float
    text: str
    origin: str
    scenario: str = ""
    section: str = ""
    title: str = ""

    def __repr__(self) -> str:
        return f"<Hit {self.chunk_id} {self.score:.4f}>"


class DenseIndex:
    def __init__(self, url: str = "http://localhost:6333") -> None:
        # Le client 1.19 avertit sur un serveur 1.12 : l'API utilisée ici
        # (query_points, upsert) est stable entre les deux.
        self.client = QdrantClient(url=url, timeout=60, check_compatibility=False)
        self.model = TextEmbedding(MODEL_NAME)

    def exists(self) -> bool:
        return self.client.collection_exists(COLLECTION)

    def count(self) -> int:
        return self.client.count(COLLECTION).count if self.exists() else 0

    def build(self, chunks_path: Path = CHUNKS, batch_size: int = 64) -> int:
        chunks = [json.loads(line) for line in chunks_path.open(encoding="utf-8")]
        if not chunks:
            raise RuntimeError(f"aucun chunk dans {chunks_path}")

        # Recréer plutôt que mettre à jour : l'index est entièrement dérivé
        # des chunks, et une collection partiellement rafraîchie mêlerait
        # deux versions du corpus sans qu'on puisse le voir.
        if self.exists():
            self.client.delete_collection(COLLECTION)
        self.client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )

        print(f"[dense] {len(chunks)} chunks à encoder")
        start = time.perf_counter()
        vectors = list(self.model.embed([c["text"] for c in chunks],
                                        batch_size=batch_size))
        elapsed = time.perf_counter() - start
        print(f"  encodage : {elapsed:.1f}s  ({len(chunks) / elapsed:.1f} chunks/s)")

        points = [
            PointStruct(
                id=i,
                vector=vec.tolist(),
                payload={
                    "chunk_id": c["chunk_id"],
                    "doc_id": c["doc_id"],
                    "text": c["text"],
                    "origin": c["origin"],
                    "strategy": c["strategy"],
                    "title": c.get("title", ""),
                    "section": c.get("section", ""),
                    "scenario": c.get("scenario", ""),
                    "url": c.get("url", ""),
                },
            )
            for i, (c, vec) in enumerate(zip(chunks, vectors))
        ]

        for i in range(0, len(points), 128):
            self.client.upsert(collection_name=COLLECTION, points=points[i : i + 128])

        total = self.count()
        print(f"  indexé : {total} points")
        return total

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        vector = next(iter(self.model.embed([QUERY_PREFIX + query])))
        response = self.client.query_points(
            collection_name=COLLECTION,
            query=vector.tolist(),
            limit=limit,
            with_payload=True,
        )
        return [
            Hit(
                chunk_id=r.payload["chunk_id"],
                doc_id=r.payload["doc_id"],
                score=r.score,
                text=r.payload["text"],
                origin=r.payload["origin"],
                scenario=r.payload.get("scenario", ""),
                section=r.payload.get("section", ""),
                title=r.payload.get("title", ""),
            )
            for r in response.points
        ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-build", action="store_true",
                   help="réutilise la collection existante")
    args = p.parse_args()

    index = DenseIndex()

    if args.skip_build and index.exists():
        print(f"[dense] collection existante : {index.count()} points")
    else:
        index.build()

    print("\n--- test ---")
    queries = [
        "database connection pool filled up and never recovered",
        "memory grew steadily with no errors reported",
        "all downstream calls slowed down at the same time",
    ]
    for q in queries:
        print(f"\n  « {q} »")
        for hit in index.search(q, limit=3):
            label = hit.scenario or hit.title[:38]
            mark = "*" if hit.origin == "sandbox" else " "
            print(f"   {mark} {hit.score:.4f}  {hit.chunk_id:<30} {label}")


if __name__ == "__main__":
    main()
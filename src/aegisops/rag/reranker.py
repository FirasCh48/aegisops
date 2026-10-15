"""Reranking par cross-encoder ONNX int8.

Dernière étape du pipeline : les moteurs dense et lexical produisent
20 candidats, le reranker les réordonne et n'en garde que 5.

La différence avec le bi-encoder est structurelle. Un bi-encoder encode
la requête et le passage séparément, puis compare deux vecteurs : il
mesure une proximité sémantique globale. Un cross-encoder lit les deux
ensemble et produit directement un score de pertinence, ce qui lui permet
de distinguer deux passages de forme identique mais de contenu opposé.

C'est exactement le cas qui fait échouer les configurations précédentes.
Le diagnostic de J4 a montré que les six premiers résultats BM25 sur les
requêtes `memory_leak` sont tous le chunk `#3-0` — la section « Observed
symptoms » — des six postmortems. Ces tableaux partagent la même
structure et les mêmes noms de métriques ; aucun réglage de poids ne les
sépare. Un cross-encoder peut, lui, comparer « memory 86 -> 381 » dans la
requête avec « memory unchanged » dans le passage.

Le coût est l'inverse du bi-encoder : celui-ci réutilise des vecteurs
calculés une fois à l'indexation, le cross-encoder recalcule tout à
chaque requête. C'est ce coût que mesure `main()`.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from aegisops.rag.dense import Hit

ROOT = Path(__file__).resolve().parents[3]
INT8_DIR = ROOT / "models" / "bge-reranker-base-onnx-int8"
FP32_DIR = ROOT / "models" / "bge-reranker-base-onnx"

# Au-delà, la paire est tronquée. Les chunks font au plus 450 tokens et
# les requêtes une centaine : 512 suffit.
MAX_LENGTH = 512


def _onnx_file_name(path: Path) -> str:
    """Le quantizer nomme sa sortie `model_quantized.onnx`, l'exporteur
    `model.onnx`. Sans cette distinction, le chargement chercherait
    `model.onnx` dans le dossier int8 et échouerait."""
    return "model_quantized.onnx" if "int8" in path.name else "model.onnx"


class Reranker:
    def __init__(self, model_dir: Path | None = None, batch_size: int = 8) -> None:
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from transformers import AutoTokenizer

        path = model_dir or (INT8_DIR if INT8_DIR.exists() else FP32_DIR)
        if not any(path.glob("*.onnx")):
            raise RuntimeError(
                f"aucun modèle ONNX dans {path}\n"
                "Lancer d'abord : uv run python scripts/export_reranker.py"
            )

        self.path = path
        self.batch_size = batch_size
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        self.model = ORTModelForSequenceClassification.from_pretrained(
            path, file_name=_onnx_file_name(path)
        )

    def score(self, query: str, passages: list[str]) -> list[float]:
        scores: list[float] = []

        for i in range(0, len(passages), self.batch_size):
            batch = passages[i : i + self.batch_size]
            inputs = self.tokenizer(
                [query] * len(batch),
                batch,
                padding=True,
                truncation=True,
                max_length=MAX_LENGTH,
                return_tensors="np",
            )
            logits = self.model(**inputs).logits
            # bge-reranker produit un logit unique par paire, non borné.
            # On le garde tel quel : seul l'ordre compte pour le
            # classement, et une sigmoïde n'ajouterait aucune information.
            scores.extend(logits.reshape(-1).tolist())

        return scores

    def rerank(self, query: str, hits: list[Hit], limit: int = 5) -> list[Hit]:
        if not hits:
            return []
        scores = self.score(query, [h.text for h in hits])
        ordered = sorted(zip(hits, scores), key=lambda hs: -hs[1])[:limit]
        return [replace(hit, score=float(score)) for hit, score in ordered]


class RerankedIndex:
    """Pipeline complet : récupération hybride, puis reranking.

    Le nombre de candidats est le paramètre qui arbitre entre qualité et
    coût : en récupérer davantage augmente les chances que le bon passage
    soit présent, mais chaque candidat supplémentaire est une paire à
    scorer.
    """

    def __init__(self, candidates: int = 20) -> None:
        from aegisops.rag.hybrid import HybridIndex

        self.hybrid = HybridIndex()
        self.reranker = Reranker()
        self.candidates = candidates

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        hits = self.hybrid.search(
            query, limit=self.candidates, candidates=self.candidates
        )
        return self.reranker.rerank(query, hits, limit=limit)


def main() -> None:
    import json

    reranker = Reranker()
    print(f"modèle : {reranker.path.name}")

    # Le coût du reranking décide s'il reste dans le pipeline : le projet
    # tourne sur CPU uniquement, et chaque investigation enchaîne
    # plusieurs recherches.
    passage = "Connection pool exhausted after the v2.8 deployment. " * 20
    print("\n--- coût du scoring ---")
    for n in (5, 10, 20):
        start = time.perf_counter()
        reranker.score("database connection pool filled up", [passage] * n)
        elapsed = time.perf_counter() - start
        print(f"  {n:>2} passages : {elapsed:>5.2f}s  ({n / elapsed:>4.1f} paires/s)")

    # La requête qui échoue sur dense seul, BM25+dense et hybride pondéré.
    # Si le reranker la résout, l'hypothèse de la session est confirmée.
    eval_file = ROOT / "eval" / "retrieval_eval.jsonl"
    query = next(
        json.loads(l) for l in eval_file.open(encoding="utf-8")
        if "212619-memory_leak" in l
    )

    print("\n--- requête en échec sur toutes les configurations précédentes ---")
    index = RerankedIndex()

    start = time.perf_counter()
    before = index.hybrid.search(query["query"], limit=5)
    hybrid_time = time.perf_counter() - start

    start = time.perf_counter()
    after = index.search(query["query"], limit=5)
    total_time = time.perf_counter() - start

    print(f"\n  hybride seul ({hybrid_time:.2f}s) :")
    for i, hit in enumerate(before, 1):
        print(f"    {i}. {hit.chunk_id}")

    print(f"\n  après reranking ({total_time:.2f}s) :")
    for i, hit in enumerate(after, 1):
        print(f"    {i}. {hit.score:>8.3f}  {hit.chunk_id}")

    print(f"\n  attendu : {query['relevant_docs'][0]}")


if __name__ == "__main__":
    main()
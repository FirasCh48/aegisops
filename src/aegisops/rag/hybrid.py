"""Fusion des classements dense et lexical par Reciprocal Rank Fusion.

RRF combine des classements, pas des scores. C'est ce qui le rend adapté
ici : le score cosinus du dense (0 à 1) et le score BM25 (non borné,
dépendant du corpus) ne sont pas comparables. Les normaliser demanderait
de choisir une échelle arbitraire, à réajuster à chaque changement de
corpus.

    score(d) = somme sur chaque moteur de  poids(moteur) / (k + rang(d))

Le RRF canonique donne le même poids aux deux moteurs. Cette hypothèse
s'est révélée fausse sur ce corpus : mesuré en J4, BM25 seul atteint
100 % de recall@5 contre 75 % pour le dense, parce que les requêtes sont
dérivées des logs et contiennent les identifiants exacts présents dans
les postmortems. À poids égal, le dense — qui place `internal_error` aux
rangs 1, 2 et 3 quand il se trompe — écrasait le bon résultat du lexical,
et la fusion tombait à 81 %.

Les poids sont donc déséquilibrés en faveur du lexical. Ce n'est pas un
réglage universel : il reflète la nature de ce corpus et de ces requêtes,
et devrait être réexaminé si l'Evidence Collector produit en semaine 4
des requêtes plus descriptives et moins riches en identifiants.
"""
from __future__ import annotations

from dataclasses import replace

from aegisops.rag.dense import DenseIndex, Hit
from aegisops.rag.sparse import SparseIndex

RRF_K = 60

# Déséquilibre assumé, justifié par la mesure (voir docstring).
ENGINE_WEIGHTS = {"dense": 0.4, "sparse": 1.0}

# On récupère plus de candidats que nécessaire chez chaque moteur : un
# document classé 15e par le dense et 3e par BM25 doit pouvoir remonter.
CANDIDATES_PER_ENGINE = 20


class HybridIndex:
    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.dense = DenseIndex()
        self.sparse = SparseIndex()
        self.sparse.load()
        self.weights = weights or ENGINE_WEIGHTS

    def _fuse(self, query: str, candidates: int) -> tuple[dict, dict, dict]:
        """Exécute les deux moteurs et calcule les scores RRF.

        Retourne les scores, les rangs par moteur et les hits indexés par
        chunk_id — de quoi produire aussi bien un classement qu'un
        diagnostic.
        """
        dense_hits = self.dense.search(query, limit=candidates)
        sparse_hits = self.sparse.search(query, limit=candidates)

        scores: dict[str, float] = {}
        ranks: dict[str, dict[str, int]] = {}
        by_id: dict[str, Hit] = {}

        for engine, hits in (("dense", dense_hits), ("sparse", sparse_hits)):
            weight = self.weights.get(engine, 1.0)
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (RRF_K + rank)
                ranks.setdefault(hit.chunk_id, {})[engine] = rank
                by_id.setdefault(hit.chunk_id, hit)

        return scores, ranks, by_id

    def search(self, query: str, limit: int = 5,
               candidates: int = CANDIDATES_PER_ENGINE) -> list[Hit]:
        scores, _, by_id = self._fuse(query, candidates)
        ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [replace(by_id[cid], score=score) for cid, score in ordered]

    def explain(self, query: str, limit: int = 5,
                candidates: int = CANDIDATES_PER_ENGINE) -> list[dict]:
        """Même recherche, en exposant la contribution de chaque moteur.

        Savoir qu'un document est remonté grâce au lexical seul, ou grâce
        aux deux, oriente l'analyse quand le recall bouge.
        """
        scores, ranks, by_id = self._fuse(query, candidates)
        ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [
            {
                "chunk_id": cid,
                "doc_id": by_id[cid].doc_id,
                "scenario": by_id[cid].scenario,
                "rrf_score": round(score, 5),
                "dense_rank": ranks[cid].get("dense"),
                "sparse_rank": ranks[cid].get("sparse"),
            }
            for cid, score in ordered
        ]


def main() -> None:
    index = HybridIndex()
    print(f"poids : {index.weights}")

    queries = [
        "process memory MB grew from 86 to 393 with no errors",
        "db_pool_timeout events and pool usage reaching 1.0",
    ]
    for q in queries:
        print(f"\n« {q} »")
        for row in index.explain(q, limit=5):
            d = row["dense_rank"] or "-"
            s = row["sparse_rank"] or "-"
            print(f"  {row['rrf_score']:.5f}  dense:{d:>3}  bm25:{s:>3}  "
                  f"{row['scenario'] or row['doc_id'][:30]}")


if __name__ == "__main__":
    main()
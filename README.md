# AegisOps AI

Système multi-agent d'investigation automatisée d'incidents SRE.
À partir de logs, métriques et postmortems historiques, il identifie la
cause racine probable et propose un plan de remédiation vérifié, soumis
à approbation humaine avant toute exécution.

**Statut :** en construction (semaine 1/8).

## Stack
Python 3.11 · FastAPI · LangGraph · Qdrant · BM25 · BGE reranker ·
Qwen2.5-1.5B fine-tuné (QLoRA) · Prometheus / Loki / Grafana

## Contrainte assumée
Développé et évalué sur CPU uniquement (Intel i5-10210U, 20 Go RAM).
Les latences mesurées reflètent cette contrainte et sont documentées
telles quelles dans `docs/benchmarks.md`.

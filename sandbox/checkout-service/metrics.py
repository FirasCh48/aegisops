"""Métriques métier, au-delà de celles fournies par l'instrumentateur HTTP."""
import os

import psutil
from prometheus_client import Counter, Gauge, Histogram

# --- Base de données -----------------------------------------------------

# Pool de connexions : sa saturation est une cause racine à part entière.
db_pool_in_use = Gauge(
    "db_pool_connections_in_use",
    "Connexions actuellement empruntées au pool",
)
db_pool_size = Gauge(
    "db_pool_size",
    "Taille maximale configurée du pool",
)

# --- Dépendances ---------------------------------------------------------

# Échecs ventilés par service et par mode de défaillance. C'est cette
# ventilation qui permet à l'agent de nommer le coupable et de qualifier
# la panne (timeout ≠ unreachable ≠ erreur applicative).
dependency_failures = Counter(
    "dependency_failures_total",
    "Échecs d'appel à une dépendance",
    ["dependency", "reason"],
)

# Buckets choisis autour du timeout applicatif (2 s) : c'est la seule
# frontière qui compte pour le diagnostic.
dependency_latency = Histogram(
    "dependency_call_duration_seconds",
    "Durée des appels aux dépendances",
    ["dependency"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

# --- Mémoire du processus ------------------------------------------------

_process = psutil.Process(os.getpid())

# La mémoire résidente est la cause racine la plus lente à se manifester.
# Sans cette gauge, une fuite n'apparaît que comme une dégradation
# progressive sans explication — voire pas du tout, puisqu'elle ne
# produit aucune erreur avant l'effondrement final.
process_memory_mb = Gauge(
    "process_memory_rss_mb",
    "Mémoire résidente du processus, en mégaoctets",
)

# Le symptôme (mémoire qui monte) et sa cause (cache qui grossit) sont
# deux métriques distinctes. La première dit qu'il y a un problème, la
# seconde dit où chercher.
cache_entries = Gauge(
    "app_cache_entries",
    "Nombre d'entrées dans le cache applicatif",
)


def refresh_memory_metrics() -> None:
    """Rafraîchit la mémoire résidente.

    Appelée à chaque requête : psutil lit /proc/self/statm, le coût est
    de l'ordre de quelques microsecondes.
    """
    process_memory_mb.set(_process.memory_info().rss / 1024 / 1024)
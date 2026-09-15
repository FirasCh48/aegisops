"""Métriques métier, au-delà de celles fournies par l'instrumentateur HTTP."""
from prometheus_client import Counter, Gauge, Histogram

# Pool de connexions : sa saturation est une cause racine à part entière
db_pool_in_use = Gauge(
    "db_pool_connections_in_use",
    "Connexions actuellement empruntées au pool",
)
db_pool_size = Gauge(
    "db_pool_size",
    "Taille maximale configurée du pool",
)

# Échecs de dépendance, ventilés par service et par mode de défaillance.
# C'est cette ventilation qui permettra à l'agent de nommer le coupable.
dependency_failures = Counter(
    "dependency_failures_total",
    "Échecs d'appel à une dépendance",
    ["dependency", "reason"],
)

# Latence des appels sortants, par dépendance
dependency_latency = Histogram(
    "dependency_call_duration_seconds",
    "Durée des appels aux dépendances",
    ["dependency"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
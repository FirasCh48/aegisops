"""Log Agent : extraction des motifs d'erreur sur la fenêtre d'incident.

Troisième nœud réel. Là où le Metrics Agent répond à « quoi et quand »,
celui-ci répond à « quoi exactement » — et c'est souvent ce qui tranche.

Les métriques de `connection_leak` et de `bad_release` sont identiques :
même saturation du pool, mêmes `db_pool_timeout`, même courbe. Seuls les
logs les séparent, par la présence d'un événement `deployment` et par le
champ `version` porté par chaque erreur.

Deux niveaux de lecture, parce que les logs du monde réel ne sont pas
tous structurés :

- les champs JSON, quand ils existent, donnent le type d'événement et
  son contexte directement ;
- le template mining regroupe les lignes de texte libre par motif, ce
  qui transforme trois cents messages en cinq motifs lisibles.

Le bac à sable produit du JSON structuré, donc le premier niveau suffit
ici. Le second existe pour que l'agent reste utilisable sur des logs
qu'on ne contrôle pas — un service tiers, une bibliothèque, un système
d'exploitation.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from aegisops.agents.state import LogObservation, TimeWindow
from aegisops.tools.loki import LogEntry, LokiClient

# Les services interrogés. L'incident est déclaré sur l'un d'eux, mais la
# cause est souvent ailleurs : en S2-J4, un 409 côté client venait d'un
# 500 d'inventory-service. Interroger les trois est la seule façon de
# remonter la chaîne.
SERVICES = ("checkout-service", "payment-service", "inventory-service")

# Événements qui décrivent le fonctionnement nominal. Les compter parmi
# les symptômes noierait le signal.
NOMINAL_EVENTS = {"order_created", "stock_reserved", "payment_authorized",
                  "service_started"}

# Événements qui ne sont pas des symptômes mais du contexte : ils
# documentent une action, pas une défaillance. Ils doivent remonter au
# rapport — un `deployment` juste avant l'incident est précisément ce qui
# distingue une régression d'une fuite — mais pas être comptés comme
# erreurs.
CONTEXT_EVENTS = {"deployment", "stock_reset"}

# Traces de l'injection elle-même : elles n'existent que dans le bac à
# sable et donneraient la réponse au Root Cause Agent. Retirées du
# résumé, conservées dans l'état pour l'analyse a posteriori.
SANDBOX_EVENTS = {"fault_injected", "fault_cleared"}
# Champs dont la valeur qualifie l'erreur. Repris dans l'échantillon pour
# que le Root Cause Agent dispose du détail sans lire les lignes brutes.
QUALIFYING_FIELDS = ("dependency", "version", "pool_size", "timeout_s",
                     "status", "error_type", "upstream", "hint", "url")


def _build_miner() -> TemplateMiner:
    """Configure drain3 sans persistance.

    L'état du miner est reconstruit à chaque investigation : conserver
    les templates d'un incident à l'autre ferait dériver le regroupement
    au fil du temps, et deux exécutions sur les mêmes données ne
    donneraient plus le même résultat.
    """
    config = TemplateMinerConfig()
    config.drain_sim_th = 0.4  # seuil de similarité pour regrouper
    config.drain_depth = 4
    config.profiling_enabled = False
    return TemplateMiner(config=config)


def _qualifying(payload: dict) -> dict:
    return {k: payload[k] for k in QUALIFYING_FIELDS if k in payload}


class LogAgent:
    def __init__(self, client: LokiClient | None = None) -> None:
        self.client = client or LokiClient()

    def observe(self, window: TimeWindow) -> list[LogObservation]:
        observations: list[LogObservation] = []

        for service in SERVICES:
            selector = f'{{service="{service}"}}'
            try:
                entries = self.client.query_range(
                    selector, window.start, window.end, limit=5000
                )
            except Exception:
                # Un service dont les logs sont inaccessibles ne doit pas
                # interrompre l'observation des autres.
                continue

            observations.extend(self._analyse(service, entries))

        return observations

    def _analyse(self, service: str, entries: list[LogEntry]) -> list[LogObservation]:
        counts: Counter = Counter()
        samples: dict[str, dict] = {}
        unstructured: list[str] = []

        for entry in entries:
            payload = entry.as_json()
            event = payload.get("event")

            if event is None:
                # Ligne non structurée : elle ira au template mining.
                unstructured.append(payload.get("_raw", entry.line))
                continue

            counts[event] += 1
            # On garde le premier échantillon de chaque type : les
            # suivants portent la même information de diagnostic, et le
            # contexte d'un modèle 1.5B est une ressource rare.
            if event not in samples:
                samples[event] = _qualifying(payload)

        out = [
            LogObservation(
                service=service,
                event=event,
                count=count,
                phase="incident",
                sample=samples.get(event, {}),
            )
            for event, count in counts.most_common()
        ]

        out.extend(self._mine(service, unstructured))
        return out

    def _mine(self, service: str, lines: list[str]) -> list[LogObservation]:
        """Regroupe les lignes non structurées par motif.

        Trois cents messages différant par un identifiant ou une durée
        deviennent un motif unique, avec son compte. C'est la seule façon
        de résumer des logs qu'on n'a pas écrits.
        """
        if not lines:
            return []

        miner = _build_miner()
        counts: Counter = Counter()
        samples: dict[int, str] = {}

        for line in lines:
            result = miner.add_log_message(line)
            cid = result["cluster_id"]
            counts[cid] += 1
            samples.setdefault(cid, line[:200])

        # Les templates sont relus à la fin, pas pendant l'ingestion :
        # drain3 généralise au fil des messages, et le template retourné
        # sur la première ligne d'un groupe est encore cette ligne
        # elle-même. Après le dernier message, chaque cluster porte son
        # motif abstrait — « failed after <*> for user <*> » plutôt que
        # « failed after 5000ms for user 1234 ».
        out = []
        for cluster in miner.drain.clusters:
            cid = cluster.cluster_id
            if cid not in counts:
                continue
            out.append(
                LogObservation(
                    service=service,
                    event=f"pattern: {cluster.get_template()[:80]}",
                    count=counts[cid],
                    phase="incident",
                    sample={"raw": samples[cid]},
                )
            )

        return sorted(out, key=lambda o: -o.count)

def summarize(observations: list[LogObservation]) -> str:
    """Résumé destiné au prompt du Root Cause Agent.

    Les événements nominaux sont retirés — leur volume écraserait le
    reste — mais les événements de contexte sont conservés : un
    `deployment` quelques secondes avant les premières erreurs est
    l'information qui sépare une régression d'une fuite préexistante.
    """
    by_service: dict[str, list[LogObservation]] = defaultdict(list)
    for o in observations:
        if o.event in NOMINAL_EVENTS or o.event in SANDBOX_EVENTS:
            continue
        by_service[o.service].append(o)

    if not by_service:
        return "aucun événement anormal"

    lines = []
    for service, items in by_service.items():
        lines.append(f"{service} :")
        for o in sorted(items, key=lambda x: -x.count):
            tag = " [contexte]" if o.event in CONTEXT_EVENTS else ""
            detail = ""
            if o.sample:
                detail = "  " + ", ".join(f"{k}={v}" for k, v in o.sample.items())
            lines.append(f"  {o.event} ×{o.count}{tag}{detail}")

    return "\n".join(lines)
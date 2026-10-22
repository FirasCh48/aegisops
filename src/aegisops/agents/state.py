"""État partagé du graphe d'investigation.

Un TypedDict plutôt qu'un modèle Pydantic : LangGraph fusionne les
dictionnaires renvoyés par chaque nœud dans l'état courant, et un
TypedDict se prête à cette fusion partielle — un nœud renvoie
`{"window": ...}` sans avoir à reconstruire l'objet entier.

Les structures portées *dans* l'état, elles, sont des modèles Pydantic :
c'est là que la validation compte, parce que ce sont elles qu'un LLM
produira à partir de S5.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class Alert(BaseModel):
    """Point d'entrée du graphe : ce qu'un système de supervision émet."""

    service: str
    summary: str
    fired_at: datetime
    # L'alerte ne porte aucune hypothèse sur la cause. C'est au graphe de
    # la construire — lui en fournir une orienterait l'investigation.
    labels: dict[str, str] = Field(default_factory=dict)


class TimeWindow(BaseModel):
    """Fenêtre d'analyse, délimitée par le Detector en J2.

    C'est le livrable le plus important des premiers nœuds : il détermine
    quelles données les agents suivants vont lire. Une fenêtre trop large
    noie le signal, une fenêtre trop étroite rate les pannes lentes.
    """

    start: datetime
    end: datetime
    # La durée conditionne ce qui est détectable : une fuite mémoire
    # demande dix minutes, une dépendance lente se voit en trente
    # secondes (mesuré en S2-J3).
    reason: str = ""

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


class MetricObservation(BaseModel):
    """Une anomalie métrique constatée, pas interprétée."""

    metric: str
    labels: dict[str, str] = Field(default_factory=dict)
    baseline: float | None = None
    peak: float | None = None
    ratio: float | None = None
    anomalous: bool = False
    note: str = ""


class LogObservation(BaseModel):
    """Un motif de log constaté sur la fenêtre."""

    service: str
    event: str
    count: int
    phase: Literal["baseline", "incident", "recovery"] = "incident"
    sample: dict = Field(default_factory=dict)


class RetrievedPassage(BaseModel):
    """Un passage du corpus, rapporté avec sa source."""

    chunk_id: str
    doc_id: str
    score: float
    text: str
    origin: str = ""
    # Section « Discriminating signal » du postmortem dont vient le
    # passage, recopiée du corpus. Seuls les postmortems du bac à sable
    # en ont une ; ailleurs, le champ reste vide. C'est ce qui permet au
    # raisonnement de séparer deux documents que BM25 ne sépare pas —
    # `connection_leak` et `bad_release` ne diffèrent que par
    # l'événement `deployment`.
    discriminating_signal: str | None = None


class IncidentContext(BaseModel):
    """Livrable de la semaine 4 : tout ce qu'un analyste aurait rassemblé.

    Volontairement dépourvu de conclusion. L'hypothèse de cause racine
    est produite en S5, par un nœud distinct, à partir de cet objet — la
    séparation garantit que le raisonnement ne puisse s'appuyer que sur
    des éléments collectés et traçables.
    """

    alert: Alert
    window: TimeWindow
    metrics: list[MetricObservation] = Field(default_factory=list)
    logs: list[LogObservation] = Field(default_factory=list)
    search_query: str = ""
    passages: list[RetrievedPassage] = Field(default_factory=list)

    def summary(self) -> str:
        anomalies = [m for m in self.metrics if m.anomalous]
        return (
            f"{self.alert.service} · fenêtre {self.window.duration_s:.0f}s · "
            f"{len(anomalies)}/{len(self.metrics)} métriques anormales · "
            f"{len(self.logs)} motifs de logs · "
            f"{len(self.passages)} passages"
        )


def append(left: list, right: list) -> list:
    """Réducteur pour les champs que plusieurs nœuds alimentent.

    Sans lui, le second nœud à écrire dans `trace` écraserait ce qu'y a
    mis le premier : LangGraph remplace par défaut.
    """
    return (left or []) + (right or [])


class IncidentState(TypedDict, total=False):
    """État partagé, traversé par tous les nœuds.

    `total=False` : chaque nœud ne renseigne que les champs qu'il produit.
    """

    alert: Alert
    window: TimeWindow
    metrics: list[MetricObservation]
    logs: list[LogObservation]
    search_query: str
    passages: list[RetrievedPassage]
    context: IncidentContext
    plan: RemediationPlan

    # Produit par le nœud `policy`. Troisième fois que ce champ est
    # oublié (hypothesis en J1, verification en J2) : le contrôle ajouté
    # dans build_graph existe pour cette raison.
    policy: PolicyReport

    # Produit par le nœud `analyse`. Un champ absent de ce TypedDict est
    # silencieusement écarté par LangGraph : le nœud le renvoie, le
    # graphe ne le reconnaît pas, et rien ne signale la perte.
    hypothesis: RootCauseHypothesis


     # Produit par le nœud `verify`. Comme pour `hypothesis` en J1 : un
    # champ absent de ce TypedDict est écarté par LangGraph sans erreur.
    verification: VerificationReport

    # Journal d'exécution. Accumulé plutôt que remplacé : c'est ce qui
    # permet de lire une investigation a posteriori, et de mesurer la
    # latence par nœud.
    trace: Annotated[list[dict], append]
    errors: Annotated[list[str], append]

class Evidence(BaseModel):
    """Une preuve citée à l'appui d'une affirmation.

    Le champ `source` doit correspondre à une observation réellement
    présente dans l'IncidentContext. C'est ce que le Verification Agent
    vérifiera en J2 : une preuve inventée est la forme la plus
    dangereuse d'hallucination, parce qu'elle rend le rapport crédible.
    """

    source: str          # "metric:db_pool_usage" ou "log:db_pool_timeout"
    observation: str     # ce que la preuve montre, en une phrase
    supports: str = ""   # quelle affirmation elle étaye


class RootCauseHypothesis(BaseModel):
    """Sortie du Root Cause Agent.

    `confidence` est demandée au modèle, pas calculée. Elle sert de
    signal, pas de garantie : un modèle sûr de lui et faux existe. Ce
    qui la rend exploitable est sa confrontation aux preuves en J2.
    """

    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    # Rempli par le parseur quand la sortie du modèle était malformée.
    # Conservé plutôt qu'écarté : une sortie partiellement récupérée
    # reste utilisable, et le champ dit au lecteur de s'en méfier.
    parse_warning: str = ""


class VerifiedClaim(BaseModel):
    """Verdict porté sur une preuve citée par le Root Cause Agent.

    `status` prend trois valeurs, et la distinction entre les deux
    premières est le point délicat :

    - "verified"    : la source existe dans le contexte et les valeurs
                      citées correspondent.
    - "misquoted"   : la source existe, mais les valeurs citées ne
                      correspondent pas à ce qui a été observé.
    - "unsupported" : la source n'existe pas. C'est une hallucination au
                      sens strict — le modèle a inventé une observation.

    Un quatrième cas existe et n'est volontairement pas traité ici : une
    preuve exacte mais sans rapport avec la cause avancée. Elle est
    marquée "verified", parce que la vérification mesure l'existence
    d'une observation, pas sa pertinence. Juger la pertinence demanderait
    de raisonner, ce qui réintroduirait précisément ce qu'on vérifie.
    """

    source: str
    status: str          # verified | misquoted | unsupported
    detail: str = ""


class VerificationReport(BaseModel):
    """Résultat du contrôle, agrégé."""

    claims: list[VerifiedClaim] = Field(default_factory=list)
    unsupported_count: int = 0
    misquoted_count: int = 0
    # Les hypothèses écartées sont vérifiées séparément : elles citent
    # souvent un signal resté normal, ce qui est une preuve d'un autre
    # type.
    ruled_out_unsupported: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.unsupported_count == 0 and self.misquoted_count == 0

    def summary(self) -> str:
        total = len(self.claims)
        ok = sum(1 for c in self.claims if c.status == "verified")
        return (
            f"{ok}/{total} preuves vérifiées, "
            f"{self.misquoted_count} mal citées, "
            f"{self.unsupported_count} sans source"
        )

class RemediationStep(BaseModel):
    """Une action proposée, avec son niveau de risque.

    Volontairement dépourvue de champ exécutable. Il n'y a pas de
    `command`, pas de `script`, pas d'`endpoint` : une étape est une
    description destinée à un humain, et l'ajout d'un champ exécutable
    demanderait d'écrire le code qui l'interprète — décision qui doit
    être prise sciemment, pas héritée d'un modèle de données.

    `risk` conditionne le traitement en J4 :

    - `read_only`    : observation, aucun effet. Consulter un tableau de
                       bord, lire un log, exécuter un profil CPU.
    - `reversible`   : modifie l'état mais se défait. Redémarrer un
                       service, revenir à une version précédente,
                       ajuster une limite.
    - `destructive`  : perte de données ou effet non réversible.
                       Supprimer, purger, basculer un trafic sans retour.
    """

    order: int
    action: str
    risk: str                    # read_only | reversible | destructive
    rationale: str = ""
    # Quelle preuve de l'hypothèse justifie cette étape. Une action sans
    # preuve rattachée sera rejetée par le policy engine en J4.
    based_on: list[str] = Field(default_factory=list)


class RemediationPlan(BaseModel):
    steps: list[RemediationStep] = Field(default_factory=list)
    # Ce que le plan ne traite pas. Un planificateur qui prétend tout
    # couvrir est plus dangereux qu'un qui nomme ses angles morts.
    not_addressed: str = ""
    parse_warning: str = ""

    def by_risk(self, level: str) -> list[RemediationStep]:
        return [s for s in self.steps if s.risk == level]

    def summary(self) -> str:
        counts = {
            level: len(self.by_risk(level))
            for level in ("read_only", "reversible", "destructive")
        }
        return (
            f"{len(self.steps)} étapes : "
            f"{counts['read_only']} lecture seule, "
            f"{counts['reversible']} réversibles, "
            f"{counts['destructive']} destructives"
        )

class PolicyDecision(BaseModel):
    """Verdict rendu sur une étape du plan."""

    order: int
    action: str
    risk: str
    decision: str              # allow | require_approval | deny
    reasons: list[str] = Field(default_factory=list)


class PolicyReport(BaseModel):
    decisions: list[PolicyDecision] = Field(default_factory=list)
    policy_version: int = 0

    @property
    def allowed(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if d.decision == "allow"]

    @property
    def pending(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if d.decision == "require_approval"]

    @property
    def denied(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if d.decision == "deny"]

    def summary(self) -> str:
        return (
            f"{len(self.allowed)} autorisées, "
            f"{len(self.pending)} en attente d'approbation, "
            f"{len(self.denied)} refusées"
        )
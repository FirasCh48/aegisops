"""Extraction de sorties structurées depuis un texte de LLM.

Les modèles de petite taille ne produisent pas du JSON fiable. Qwen2.5
1.5B, qui servira en S6, ajoute des préambules, oublie des virgules,
ferme mal les accolades. Un parseur strict rejetterait des sorties dont
l'essentiel est exploitable.

L'approche retenue est en cascade : chaque niveau récupère ce que le
précédent n'a pas su lire, et le dernier recours est une extraction par
motifs. Ce qui n'a pas pu être lu au niveau le plus strict est signalé
dans `parse_warning`, pour que le lecteur sache ce qui a été deviné.

Écrit maintenant plutôt qu'en S6 : un parseur ajouté après coup est
ajusté aux défauts d'un modèle particulier, ce qui le rend inutilisable
pour les autres.
"""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Bloc JSON délimité par des backticks, avec ou sans étiquette de langue.
FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
# Premier objet JSON de niveau racine, pour les sorties sans backticks.
BARE = re.compile(r"(\{.*\})", re.DOTALL)


def _try_json(text: str) -> dict | None:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _repair(text: str) -> str:
    """Corrige les défauts de syntaxe les plus fréquents.

    Chaque règle vient d'une sortie réellement observée, pas d'une
    anticipation. Les ajouter sans preuve reviendrait à masquer des
    erreurs qui mériteraient d'être vues.
    """
    # Virgule avant une fermeture : {"a": 1,} ou [1, 2,]
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    # Guillemets typographiques, fréquents quand le modèle « rédige »
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return text


def parse_structured(text: str, model: type[T]) -> tuple[T | None, str]:
    """Extrait une instance de `model` depuis la sortie d'un LLM.

    Renvoie l'instance et un avertissement décrivant le niveau auquel
    l'extraction a réussi. Un avertissement vide signifie que la sortie
    était directement valide.
    """
    candidates: list[tuple[str, str]] = []

    direct = _try_json(text.strip())
    if direct is not None:
        candidates.append(("", json.dumps(direct)))

    for match in FENCED.findall(text):
        candidates.append(("extrait d'un bloc délimité", match))

    match = BARE.search(text)
    if match:
        candidates.append(("extrait du premier objet trouvé", match.group(1)))

    for level, raw in candidates:
        for repaired, note in ((raw, level), (_repair(raw), f"{level}, après réparation syntaxique")):
            payload = _try_json(repaired)
            if payload is None:
                continue
            try:
                return model(**payload), note.strip(", ")
            except ValidationError:
                # JSON valide mais champs manquants ou mal typés : on
                # continue, un candidat suivant peut être complet.
                continue

    return None, "aucune sortie structurée exploitable"
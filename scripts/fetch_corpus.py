#!/usr/bin/env python3
"""Télécharge et nettoie un corpus de postmortems publics.

Le corpus sert de mémoire historique au RAG : l'agent doit pouvoir
rapprocher un incident du bac à sable d'un incident réel documenté
publiquement.

Usage:
    uv run python scripts/fetch_corpus.py
    uv run python scripts/fetch_corpus.py --limit 20
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data" / "corpus"

# Dépôts GitHub dont le README est une liste curée de postmortems.
# On récupère le README brut, puis on suit les liens externes.
SOURCES = [
    {
        "name": "danluu-post-mortems",
        "url": "https://raw.githubusercontent.com/danluu/post-mortems/master/README.md",
    },
    {
        "name": "howtheysre",
        "url": "https://raw.githubusercontent.com/upgundecha/howtheysre/master/README.md",
    },
]

# Un postmortem utile pour ce projet parle de panne d'infrastructure.
# Ce filtre écarte les liens hors sujet (conférences, outils, livres).
RELEVANT_TERMS = re.compile(
    r"outage|incident|postmortem|post-mortem|downtime|degradation|"
    r"failure|disruption|rca|root cause",
    re.IGNORECASE,
)

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


@dataclass
class Document:
    doc_id: str
    title: str
    url: str
    source: str
    text: str
    meta: dict = field(default_factory=dict)


def fetch(url: str, timeout: float = 20.0) -> str | None:
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "aegisops-corpus/0.1"})
        r.raise_for_status()
        return r.text
    except httpx.HTTPError:
        return None


def extract_links(markdown: str, source: str) -> list[dict]:
    """Extrait les liens pertinents d'un README de curation."""
    seen, out = set(), []
    for title, url in LINK_RE.findall(markdown):
        if url in seen:
            continue
        seen.add(url)
        # Le titre porte souvent le contexte ; l'URL rarement.
        if not RELEVANT_TERMS.search(title) and not RELEVANT_TERMS.search(url):
            continue
        out.append({"title": title.strip(), "url": url, "source": source})
    return out


def html_to_text(html: str) -> str:
    """Conversion minimale. Les pages d'incident sont très hétérogènes :
    plutôt que de parser finement chaque format, on extrait le texte et on
    laisse le chunking de J2 faire le découpage."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Normalise les lignes vides multiples laissées par la suppression
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=60,
                   help="nombre maximum de documents à télécharger")
    p.add_argument("--min-chars", type=int, default=800,
                   help="longueur minimale d'un document retenu")
    args = p.parse_args()

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    candidates: list[dict] = []
    for src in SOURCES:
        print(f"[index] {src['name']}")
        md = fetch(src["url"])
        if not md:
            print(f"  échec : {src['url']}")
            continue
        links = extract_links(md, src["name"])
        print(f"  {len(links)} liens pertinents")
        candidates.extend(links)

    print(f"\n{len(candidates)} candidats, limite à {args.limit}\n")

    documents: list[Document] = []
    for i, cand in enumerate(candidates[: args.limit], 1):
        print(f"[{i}/{min(len(candidates), args.limit)}] {cand['title'][:60]}")
        html = fetch(cand["url"])
        if not html:
            print("    inaccessible")
            continue

        text = html_to_text(html)
        # Une page trop courte est une redirection, un paywall ou une
        # erreur : elle n'apporte rien et pollue l'index.
        if len(text) < args.min_chars:
            print(f"    trop court ({len(text)} car.)")
            continue

        doc_id = f"PUB-{i:03d}"
        documents.append(Document(
            doc_id=doc_id,
            title=cand["title"],
            url=cand["url"],
            source=cand["source"],
            text=text,
            meta={"chars": len(text)},
        ))
        # Politesse : ces sites n'ont rien demandé.
        time.sleep(0.5)

    out = CORPUS_DIR / "public_postmortems.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for doc in documents:
            f.write(json.dumps({
                "doc_id": doc.doc_id,
                "title": doc.title,
                "url": doc.url,
                "source": doc.source,
                "origin": "public",
                "text": doc.text,
                **doc.meta,
            }, ensure_ascii=False) + "\n")

    total = sum(d.meta["chars"] for d in documents)
    print(f"\n{len(documents)} documents, {total // 1000} k caractères")
    print(f"écrit : {out}")


if __name__ == "__main__":
    main()
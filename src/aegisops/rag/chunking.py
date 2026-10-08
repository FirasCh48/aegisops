"""Découpage du corpus en chunks indexables.

Deux stratégies, parce que le corpus contient deux familles de documents
qui n'ont rien en commun :

- les postmortems du bac à sable ont une structure fixe en sept sections.
  Les découper par section préserve l'unité de sens : « Investigation »
  entière vaut mieux que trois fragments arbitraires.

- les postmortems publics sont du texte extrait de HTML, sans structure
  fiable. Ils sont découpés par fenêtre glissante sur les paragraphes.

La stratégie employée est enregistrée dans les métadonnées. Si le recall
diffère entre les deux familles en J6, on saura si la cause est le
découpage ou le contenu.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = ROOT / "data" / "corpus"

TARGET_TOKENS = 380
OVERLAP_TOKENS = 50
# bge-small-en-v1.5 tronque au-delà de 512 tokens, sans erreur ni
# avertissement. Un chunk plus long serait indexé amputé et personne ne
# le saurait. La marge absorbe l'écart entre ce tokenizer (BPE) et celui
# du modèle (WordPiece), de l'ordre de 10 %.
MAX_TOKENS = 450
MIN_TOKENS = 40

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    tokens: int
    origin: str          # "sandbox" ou "public"
    strategy: str        # "section" ou "window"
    title: str = ""
    section: str = ""
    url: str = ""
    scenario: str = ""
    meta: dict = field(default_factory=dict)


def enforce_limit(pieces: list[str], prefix: str = "") -> list[str]:
    """Garantit qu'aucun chunk ne dépasse MAX_TOKENS, préfixe compris.

    Les découpages précédents raisonnent sur des frontières naturelles —
    paragraphes, puis phrases — et peuvent dépasser la cible quand aucune
    frontière ne tombe au bon endroit. Cette fonction est le dernier
    recours : elle coupe au token près, en acceptant de casser une phrase
    plutôt que de laisser passer un chunk qui serait tronqué à
    l'indexation.
    """
    budget = MAX_TOKENS - count_tokens(prefix) if prefix else MAX_TOKENS
    if budget < MIN_TOKENS:
        budget = MIN_TOKENS

    out: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= budget:
            out.append(piece)
            continue
        ids = _enc.encode(piece)
        for i in range(0, len(ids), budget):
            out.append(_enc.decode(ids[i : i + budget]))
    return out


def split_by_window(text: str, target: int, overlap: int) -> list[str]:
    """Fenêtre glissante sur les paragraphes.

    Le découpage se fait sur les frontières de paragraphe plutôt qu'au
    token près : couper une phrase en deux produit deux fragments dont
    aucun ne porte l'idée complète.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = count_tokens(para)

        # Un paragraphe seul peut dépasser la cible : on le coupe par
        # phrases plutôt que de l'indexer tronqué.
        if para_tokens > target:
            if current:
                chunks.append("\n\n".join(current))
                current, current_tokens = [], 0
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf, buf_tokens = [], 0
            for sent in sentences:
                st = count_tokens(sent)
                if buf_tokens + st > target and buf:
                    chunks.append(" ".join(buf))
                    buf, buf_tokens = [], 0
                buf.append(sent)
                buf_tokens += st
            if buf:
                chunks.append(" ".join(buf))
            continue

        if current_tokens + para_tokens > target and current:
            chunks.append("\n\n".join(current))
            # Recouvrement : on reprend les derniers paragraphes jusqu'à
            # atteindre la taille d'overlap. Sans lui, une idée à cheval
            # sur deux chunks n'est retrouvable dans aucun des deux.
            back, back_tokens = [], 0
            for prev in reversed(current):
                pt = count_tokens(prev)
                if back_tokens + pt > overlap:
                    break
                back.insert(0, prev)
                back_tokens += pt
            current, current_tokens = back, back_tokens

        current.append(para)
        current_tokens += para_tokens

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def split_by_section(markdown: str) -> list[tuple[str, str]]:
    """Découpe un markdown sur les titres de niveau 2.

    Retourne des couples (titre de section, contenu).
    """
    sections: list[tuple[str, list[str]]] = []
    current_title = "preamble"
    current: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("## "):
            if current:
                sections.append((current_title, current))
            current_title = line[3:].strip()
            current = []
        else:
            current.append(line)

    if current:
        sections.append((current_title, current))

    return [(t, "\n".join(c).strip()) for t, c in sections]


def chunk_sandbox_postmortems() -> list[Chunk]:
    chunks: list[Chunk] = []
    files = sorted((CORPUS_DIR / "sandbox").glob("*.md"))

    if not files:
        print("  absent : data/corpus/sandbox/")
        return chunks

    for path in files:
        scenario = path.stem
        text = path.read_text(encoding="utf-8")

        title = next(
            (l[2:].strip() for l in text.splitlines() if l.startswith("# ")),
            scenario,
        )

        for i, (section, content) in enumerate(split_by_section(text)):
            if not content:
                continue
            if count_tokens(content) < MIN_TOKENS:
                continue

            # Chaque chunk rappelle son titre et sa section. Isolé dans
            # l'index, un chunk ne dirait sinon pas de quel incident il
            # parle — ni même qu'il s'agit d'un incident.
            prefix = f"{title} — {section}\n\n"

            pieces = split_by_window(content, TARGET_TOKENS, OVERLAP_TOKENS)
            pieces = enforce_limit(pieces, prefix)

            for j, piece in enumerate(pieces):
                if count_tokens(piece) < MIN_TOKENS:
                    continue
                full = prefix + piece
                chunks.append(Chunk(
                    chunk_id=f"sandbox/{scenario}.md#{i}-{j}",
                    doc_id=f"sandbox/{scenario}.md",
                    text=full,
                    tokens=count_tokens(full),
                    origin="sandbox",
                    strategy="section",
                    title=title,
                    section=section,
                    scenario=scenario,
                ))

    return chunks


def chunk_public_corpus() -> list[Chunk]:
    chunks: list[Chunk] = []
    path = CORPUS_DIR / "public_postmortems.jsonl"
    if not path.exists():
        print(f"  absent : {path}")
        return chunks

    for line in path.open(encoding="utf-8"):
        doc = json.loads(line)
        prefix = f"{doc['title']}\n\n"

        pieces = split_by_window(doc["text"], TARGET_TOKENS, OVERLAP_TOKENS)
        pieces = enforce_limit(pieces, prefix)

        for j, piece in enumerate(pieces):
            if count_tokens(piece) < MIN_TOKENS:
                continue
            full = prefix + piece
            chunks.append(Chunk(
                chunk_id=f"{doc['doc_id']}#{j}",
                doc_id=doc["doc_id"],
                text=full,
                tokens=count_tokens(full),
                origin="public",
                strategy="window",
                title=doc["title"],
                url=doc.get("url", ""),
                meta={"source": doc.get("source", "")},
            ))

    return chunks


def build() -> list[Chunk]:
    print("[chunk] postmortems du bac à sable")
    sandbox = chunk_sandbox_postmortems()
    print(f"  {len(sandbox)} chunks")

    print("[chunk] corpus public")
    public = chunk_public_corpus()
    print(f"  {len(public)} chunks")

    return sandbox + public


def main() -> None:
    chunks = build()
    out = CORPUS_DIR / "chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    total = len(chunks)
    tokens = sorted(c.tokens for c in chunks)
    print(f"\n{total} chunks")
    print(f"  tokens : min {tokens[0]}  médiane {tokens[total // 2]}  "
          f"max {tokens[-1]}")
    print(f"  sandbox : {sum(1 for c in chunks if c.origin == 'sandbox')}")
    print(f"  public  : {sum(1 for c in chunks if c.origin == 'public')}")

    over = [c for c in chunks if c.tokens > MAX_TOKENS]
    if over:
        print(f"\nATTENTION : {len(over)} chunks dépassent {MAX_TOKENS} tokens "
              f"et seraient tronqués à l'indexation")
        for c in over[:5]:
            print(f"  {c.chunk_id} : {c.tokens}")
    else:
        print(f"  aucun chunk au-dessus de {MAX_TOKENS} tokens")

    # Les 6 postmortems du bac à sable sont les seuls documents dont on
    # sait qu'ils répondent aux requêtes d'évaluation. Vérifier que chacun
    # a produit des chunks évite d'indexer un corpus amputé sans le voir.
    scenarios = {c.scenario for c in chunks if c.origin == "sandbox"}
    expected = {
        "connection_leak", "memory_leak", "slow_response",
        "bad_release", "cpu_saturation", "internal_error",
    }
    missing = expected - scenarios
    if missing:
        print(f"\nATTENTION : scénarios sans chunk : {sorted(missing)}")

    print(f"\nécrit : {out}")


if __name__ == "__main__":
    main()
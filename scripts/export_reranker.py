#!/usr/bin/env python3
"""Exporte bge-reranker-base en ONNX quantifié int8.

Le cross-encoder est le composant le plus coûteux du pipeline : il calcule
un score par paire (requête, passage), là où le bi-encoder réutilise des
vecteurs calculés une fois pour toutes à l'indexation.

La quantification int8 remplace les poids flottants 32 bits par des
entiers 8 bits. Elle divise la taille du modèle par environ quatre et
accélère l'inférence CPU, au prix d'une perte de précision — à mesurer,
pas à supposer.

Usage:
    uv run python scripts/export_reranker.py
    uv run python scripts/export_reranker.py --force
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
MODEL_NAME = "BAAI/bge-reranker-base"
FP32_DIR = MODELS_DIR / "bge-reranker-base-onnx"
INT8_DIR = MODELS_DIR / "bge-reranker-base-onnx-int8"


def has_onnx(path: Path) -> bool:
    """Un dossier peut exister et être vide — cas d'une interruption en
    cours d'export, ou d'une étape qui a échoué après avoir créé son
    répertoire de sortie. Vérifier le fichier produit, pas le répertoire.
    """
    return path.exists() and any(path.glob("*.onnx"))


def dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def export_fp32() -> None:
    from optimum.onnxruntime import ORTModelForSequenceClassification
    from transformers import AutoTokenizer

    print(f"[export] {MODEL_NAME} -> ONNX fp32")
    print("  premier appel : téléchargement de ~1,1 Go puis conversion,")
    print("  comptez une dizaine de minutes sans affichage intermédiaire")
    start = time.perf_counter()

    model = ORTModelForSequenceClassification.from_pretrained(
        MODEL_NAME, export=True
    )
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    FP32_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(FP32_DIR)
    tokenizer.save_pretrained(FP32_DIR)

    print(f"  {time.perf_counter() - start:.1f}s  —  {dir_size_mb(FP32_DIR):.0f} Mo")


def quantize_int8() -> None:
    from optimum.onnxruntime import ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig

    print("[quant] fp32 -> int8")
    start = time.perf_counter()

    quantizer = ORTQuantizer.from_pretrained(FP32_DIR)
    # Comet Lake (i5-10210U) n'a pas AVX-512 : avx2 est le jeu
    # d'instructions disponible. Choisir avx512_vnni ici produirait un
    # modèle qui retomberait silencieusement sur un chemin plus lent.
    #
    # is_static=False : quantification dynamique, qui ne demande pas de
    # jeu de calibration — plus simple, et suffisante pour un
    # cross-encoder dont on ne cherche pas le dernier point de précision.
    config = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)

    INT8_DIR.mkdir(parents=True, exist_ok=True)
    quantizer.quantize(save_dir=INT8_DIR, quantization_config=config)

    # Le quantizer ne copie que le graphe ONNX. Sans ces fichiers, le
    # chargement échouerait au tokenizer, bien après la quantification.
    for name in ("tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "vocab.txt", "config.json"):
        src = FP32_DIR / name
        if src.exists():
            shutil.copy(src, INT8_DIR / name)

    print(f"  {time.perf_counter() - start:.1f}s  —  {dir_size_mb(INT8_DIR):.0f} Mo")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--force", action="store_true",
                   help="refait l'export et la quantification")
    args = p.parse_args()

    if args.force:
        for path in (FP32_DIR, INT8_DIR):
            if path.exists():
                shutil.rmtree(path)
                print(f"[clean] supprimé : {path.name}")

    if not has_onnx(FP32_DIR):
        export_fp32()
    else:
        print(f"[export] déjà présent : {FP32_DIR.name}")

    if not has_onnx(INT8_DIR):
        quantize_int8()
    else:
        print(f"[quant] déjà présent : {INT8_DIR.name}")

    print()
    ok = True
    for label, path in (("fp32", FP32_DIR), ("int8", INT8_DIR)):
        if not has_onnx(path):
            print(f"{label} : ÉCHEC — aucun .onnx dans {path}")
            ok = False
            continue
        files = sorted(f.name for f in path.glob("*.onnx"))
        print(f"{label} : {dir_size_mb(path):>6.0f} Mo  —  {files}")

    if ok and has_onnx(FP32_DIR) and has_onnx(INT8_DIR):
        ratio = dir_size_mb(FP32_DIR) / dir_size_mb(INT8_DIR)
        print(f"\nréduction : {ratio:.1f}×")


if __name__ == "__main__":
    main()
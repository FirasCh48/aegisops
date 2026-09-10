"""Benchmark tok/s des modèles Ollama locaux."""
import json
import httpx

BASE = "http://localhost:11434"
MODELS = [
    "qwen2.5:1.5b-instruct-q4_K_M",
    "qwen2.5:3b-instruct-q4_K_M",
]
PROMPT = (
    "You are an SRE assistant. Analyze this alert and give a short "
    "probable root cause:\n"
    "ALERT: checkout-service error rate 15%, DB connections 100/100, "
    "p95 latency 4.8s, CPU 42%, memory 61%."
)


def bench(model: str) -> dict:
    r = httpx.post(
        f"{BASE}/api/generate",
        json={
            "model": model,
            "prompt": PROMPT,
            "stream": False,
            "options": {"num_predict": 200, "temperature": 0.1},
        },
        timeout=900.0,
    )
    r.raise_for_status()
    d = r.json()
    eval_s = d["eval_duration"] / 1e9
    load_s = d.get("load_duration", 0) / 1e9
    prompt_s = d.get("prompt_eval_duration", 0) / 1e9
    return {
        "model": model,
        "tokens": d["eval_count"],
        "gen_s": round(eval_s, 2),
        "tok_s": round(d["eval_count"] / eval_s, 2),
        "load_s": round(load_s, 2),
        "prompt_s": round(prompt_s, 2),
        "total_s": round(d["total_duration"] / 1e9, 2),
    }


if __name__ == "__main__":
    results = []
    for m in MODELS:
        print(f"--> {m} ...", flush=True)
        try:
            res = bench(m)
            results.append(res)
            print(json.dumps(res, indent=2))
        except Exception as e:
            print(f"ERREUR {m}: {e}")
    print("\n| Modèle | tokens | gen (s) | tok/s | total (s) |")
    print("| --- | --- | --- | --- | --- |")
    for r in results:
        print(
            f"| {r['model']} | {r['tokens']} | {r['gen_s']} "
            f"| {r['tok_s']} | {r['total_s']} |"
        )

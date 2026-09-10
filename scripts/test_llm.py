"""Vérifie que les trois providers répondent."""
from aegisops.core.llm import get_llm

PROMPT = "In one short sentence: what causes database connection pool exhaustion?"

for p in ["local", "groq", "gemini"]:
    try:
        out = get_llm(p).complete(PROMPT, max_tokens=80)
        print(f"[OK] {p}: {out.strip()[:160]}")
    except Exception as e:
        print(f"[KO] {p}: {type(e).__name__}: {e}")

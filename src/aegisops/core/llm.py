"""Provider LLM commutable : local (Ollama) / groq / gemini."""
from abc import ABC, abstractmethod

import httpx

from aegisops.core.config import settings


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        """Retourne le texte généré."""


class OllamaProvider(LLMProvider):
    name = "ollama"

    def complete(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        r = httpx.post(
            f"{settings.ollama_base_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": max_tokens, "temperature": 0.1},
            },
            timeout=900.0,
        )
        r.raise_for_status()
        return r.json()["response"]


class GroqProvider(LLMProvider):
    name = "groq"

    def complete(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        from groq import Groq

        client = Groq(api_key=settings.groq_api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        return resp.choices[0].message.content


class GeminiProvider(LLMProvider):
    name = "gemini"

    def complete(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system or None,
                max_output_tokens=max(max_tokens, 512),
                temperature=0.1,
                           ),
        )
        return resp.text


_PROVIDERS = {
    "local": OllamaProvider,
    "ollama": OllamaProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
}


def get_llm(provider: str | None = None) -> LLMProvider:
    key = (provider or settings.llm_provider).lower()
    if key not in _PROVIDERS:
        raise ValueError(
            f"Provider inconnu: {key}. Choix: {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[key]()

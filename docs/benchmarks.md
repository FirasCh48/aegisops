# Benchmarks
## Providers configurés (sept. 2026)

| Provider | Modèle | Usage |
| --- | --- | --- |
| local (Ollama) | qwen2.5:1.5b / 3b instruct Q4_K_M | benchmarks finaux, démo du modèle fine-tuné |
| groq | openai/gpt-oss-120b | développement, génération du dataset, juge RAGAS |
| gemini | gemini-3.6-flash | second juge, génération de variantes |

Les noms de modèles changent régulièrement côté fournisseurs. Ils sont
donc lus depuis `.env` et jamais codés en dur.
## Matériel
Intel i5-10210U (4c/8t) · 20 Go RAM (WSL2 plafonné à 14 Go) · NVIDIA MX130 2 Go (inutilisée)

## Inférence locale (Ollama, quantization Q4_K_M)

Prompt : alerte SRE réaliste · num_predict=200 · temperature=0.1

<colli il tableau eli tal3 mil script>

## Conséquence
Le développement se fait via Groq. L'inférence locale est réservée
aux benchmarks finaux et à la démonstration du modèle fine-tuné.# Benchmarks

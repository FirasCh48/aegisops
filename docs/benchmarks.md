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



## Retrieval — dense, lexical et fusion (S3-J4)

| Config | recall@5 | MRR | latence |
| --- | --- | --- | --- |
| dense seul | 75,0 % | 0,615 | 88 ms |
| BM25 seul | **100,0 %** | 0,584 | **3 ms** |
| hybride RRF (0,4 / 1,0) | 87,5 % | 0,558 | 131 ms |

### Pourquoi BM25 domine

Les 16 requêtes sont dérivées des logs d'incidents et contiennent les
identifiants exacts présents dans les postmortems. La tâche est lexicale
avant d'être sémantique. Ce résultat qualifie le jeu d'évaluation autant
que le moteur.

### Pourquoi la fusion perd

En RRF à poids égal, l'hybride tombait à 81,2 % : le dense place
`internal_error` aux rangs 1, 2 et 3 quand il se trompe, et écrase le bon
résultat du lexical. Déséquilibrer les poids (0,4 pour le dense) récupère
six points.

### Les deux échecs restants

Les deux requêtes `memory_leak` échouent sur toutes les configurations
hybrides. Le diagnostic montre que les six premiers résultats BM25 sont
tous le chunk `#3-0` — la section « Observed symptoms » — des six
postmortems. Ces tableaux partagent la même structure et les mêmes noms
de métriques ; une requête composée de chiffres et d'identifiants leur
ressemble également.

La cause est structurelle, pas paramétrique : aucun réglage de poids ne
distingue six tableaux de même forme. Le reranker de J5 est le niveau
auquel ce problème se traite — un cross-encoder lit la requête et le
passage ensemble, et peut séparer un tableau où la mémoire passe de 86 à
381 Mo d'un tableau où elle est inchangée.


## Retrieval — ablation complète (S3-J5)

| Config | recall@5 | MRR | latence |
| --- | --- | --- | --- |
| dense seul | 75,0 % | 0,615 | 52 ms |
| **BM25 seul** | **100,0 %** | 0,584 | **1,2 ms** |
| hybride RRF (0,4 / 1,0) | 87,5 % | 0,558 | 69 ms |
| hybride + reranking int8 | 62,5 % | 0,513 | 11 326 ms |

### Le reranking dégrade le résultat

Le cross-encoder `bge-reranker-base`, exporté en ONNX et quantifié int8
(1 129 Mo → 296 Mo, réduction 3,8×), fait perdre 25 points de recall pour
un coût de 11 secondes par requête — soit 160 fois la latence de
l'hybride.

Les six échecs renvoient tous `internal_error` aux trois premiers rangs,
quel que soit le scénario attendu. Le diagnostic sur une requête
`memory_leak` montre des scores de 0,70 / 0,61 / 0,22 pour les passages
`internal_error` contre −2,34 et −2,90 pour les passages `memory_leak`,
alors que ces derniers figuraient bien parmi les 20 candidats fournis.
Le modèle ne les écarte pas faute de les voir : il les rejette.

### Pourquoi

Les requêtes d'évaluation sont composées de données structurées — des
dictionnaires d'événements et des couples de valeurs métriques :

    Baseline events: {'order_created': 135}
    process memory MB: 85.7734 -> 381.2852

Un cross-encoder est entraîné sur des paires (question en langue
naturelle, passage de texte). Confronté à une structure de données face à
de la prose, il cherche une correspondance sémantique qui n'existe pas, et
converge vers le postmortem au vocabulaire le plus générique —
`internal_error` décrit un système « à moitié cassé », ce qui ressemble
vaguement à tout.

### Décision

Le reranking est écarté du pipeline. La configuration retenue est
l'hybride RRF, malgré un recall inférieur à BM25 seul : le MRR et le
comportement sur les requêtes descriptives restent à départager en J6,
avec le second jeu d'évaluation.

Ce résultat ne condamne pas le reranking en
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

Ce résultat ne condamne pas le reranking en général. Il indique que la
forme des requêtes produites par l'Evidence Collector détermine le choix
du moteur — point à réexaminer en semaine 4 si les requêtes deviennent
descriptives.


## Retrieval — synthèse (S3-J6)

### Deux profils de requête, deux gagnants

| Config | 16 requêtes incident | 10 requêtes descriptives | total |
| --- | --- | --- | --- |
| dense seul | 75,0 % | X % | X % |
| BM25 seul | 100,0 % | Y % | Y % |
| hybride RRF | 87,5 % | Z % | Z % |

Les requêtes dérivées des incidents sont composées de données
structurées et contiennent les identifiants exacts du corpus : BM25 y
atteint 100 %. Les requêtes descriptives reformulent les symptômes sans
reprendre le vocabulaire des documents : [conclusion à écrire].

### Configuration retenue

[à écrire après mesure]

### Ce que mesure chaque jeu

Le premier jeu reflète ce que produira l'Evidence Collector en semaine 4
s'il transmet les logs bruts. Le second reflète ce qu'il produirait s'il
résumait l'incident en langue naturelle.

Le choix du moteur dépend donc d'une décision d'architecture encore
ouverte, et non d'une propriété intrinsèque des moteurs.


## Retrieval — synthèse et décision (S3-J6)

### Deux jeux d'évaluation, deux profils de requête

| Config | 16 requêtes incident | 10 requêtes descriptives | latence |
| --- | --- | --- | --- |
| dense seul | 75,0 % | 50,0 % | 20–88 ms |
| **BM25 seul** | **100,0 %** | **80,0 %** | **1,1 ms** |
| hybride RRF (0,4) | 87,5 % | 70,0 % | 49–69 ms |
| hybride RRF (0,15) | 87,5 % | 70,0 % | 28–76 ms |
| hybride + reranking | 62,5 % | non mesuré | 11 326 ms |

### L'hypothèse était fausse

Les 16 requêtes dérivées des incidents contiennent les identifiants
exacts du corpus (`db_pool_timeout`, `process_memory_rss_mb`), ce qui
expliquait le score parfait de BM25. L'hypothèse était que le dense
reprendrait l'avantage sur des requêtes descriptives, reformulées sans
reprendre le vocabulaire des documents.

Le second jeu l'infirme : BM25 obtient 80 % contre 50 % pour le dense.

La raison tient au domaine. Un corpus de postmortems d'infrastructure
utilise un vocabulaire restreint — `load balancer`, `backup`, `prefix`,
`advertisement`, `rollback`. Même une requête reformulée réutilise deux
ou trois de ces termes, et deux termes rares suffisent à BM25. Le dense,
lui, voit « un système est tombé en panne » dans les 41 documents et ne
les sépare pas.

### Le dense n'apporte rien à la fusion

Abaisser son poids de 0,4 à 0,15 ne change **aucun** résultat : mêmes
recalls, mêmes échecs, aux mêmes rangs. Le moteur dense n'influence plus
le classement, il ne fait qu'ajouter 76 ms.

### Configuration retenue : BM25 seul

| Critère | BM25 | hybride |
| --- | --- | --- |
| recall (incident) | 100 % | 87,5 % |
| recall (descriptif) | 80 % | 70 % |
| latence | 1,1 ms | 76 ms |
| dépendances | `bm25s` | + Qdrant, fastembed, qdrant-client |
| temps d'indexation | 0,4 s | 83 s |

Le code dense et hybride est conservé : il constitue la mesure qui
justifie la décision, et servira aux ablations de S7.

### Limite de cette conclusion

Elle vaut pour ce corpus — 41 documents, dont 12 sans contenu
exploitable (pages de statut sans description d'incident) — et pour ces
deux profils de requête. Un corpus plus large, ou des requêtes produites
par l'Evidence Collector sous une forme encore inconnue, pourraient
inverser le résultat. La mesure est à refaire en semaine 4 une fois les
requêtes réelles connues.
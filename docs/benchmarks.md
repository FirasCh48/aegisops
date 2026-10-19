# Benchmarks

## Providers configurés (sept. 2026)

| Provider       | Modèle                            | Usage                                            |
| -------------- | --------------------------------- | ------------------------------------------------ |
| local (Ollama) | qwen2.5:1.5b / 3b instruct Q4_K_M | benchmarks finaux, démo du modèle fine-tuné      |
| groq           | openai/gpt-oss-120b               | développement, génération du dataset, juge RAGAS |
| gemini         | gemini-3.6-flash                  | second juge, génération de variantes             |

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

| Config                  | recall@5    | MRR   | latence  |
| ----------------------- | ----------- | ----- | -------- |
| dense seul              | 75,0 %      | 0,615 | 88 ms    |
| BM25 seul               | **100,0 %** | 0,584 | **3 ms** |
| hybride RRF (0,4 / 1,0) | 87,5 %      | 0,558 | 131 ms   |

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

| Config                   | recall@5    | MRR   | latence    |
| ------------------------ | ----------- | ----- | ---------- |
| dense seul               | 75,0 %      | 0,615 | 52 ms      |
| **BM25 seul**            | **100,0 %** | 0,584 | **1,2 ms** |
| hybride RRF (0,4 / 1,0)  | 87,5 %      | 0,558 | 69 ms      |
| hybride + reranking int8 | 62,5 %      | 0,513 | 11 326 ms  |

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

| Config      | 16 requêtes incident | 10 requêtes descriptives | total |
| ----------- | -------------------- | ------------------------ | ----- |
| dense seul  | 75,0 %               | X %                      | X %   |
| BM25 seul   | 100,0 %              | Y %                      | Y %   |
| hybride RRF | 87,5 %               | Z %                      | Z %   |

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

| Config              | 16 requêtes incident | 10 requêtes descriptives | latence    |
| ------------------- | -------------------- | ------------------------ | ---------- |
| dense seul          | 75,0 %               | 50,0 %                   | 20–88 ms   |
| **BM25 seul**       | **100,0 %**          | **80,0 %**               | **1,1 ms** |
| hybride RRF (0,4)   | 87,5 %               | 70,0 %                   | 49–69 ms   |
| hybride RRF (0,15)  | 87,5 %               | 70,0 %                   | 28–76 ms   |
| hybride + reranking | 62,5 %               | non mesuré               | 11 326 ms  |

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

| Critère             | BM25    | hybride                            |
| ------------------- | ------- | ---------------------------------- |
| recall (incident)   | 100 %   | 87,5 %                             |
| recall (descriptif) | 80 %    | 70 %                               |
| latence             | 1,1 ms  | 76 ms                              |
| dépendances         | `bm25s` | + Qdrant, fastembed, qdrant-client |
| temps d'indexation  | 0,4 s   | 83 s                               |

Le code dense et hybride est conservé : il constitue la mesure qui
justifie la décision, et servira aux ablations de S7.

### Limite de cette conclusion

Elle vaut pour ce corpus — 41 documents, dont 12 sans contenu
exploitable (pages de statut sans description d'incident) — et pour ces
deux profils de requête. Un corpus plus large, ou des requêtes produites
par l'Evidence Collector sous une forme encore inconnue, pourraient
inverser le résultat. La mesure est à refaire en semaine 4 une fois les
requêtes réelles connues.

## Détecteur de fenêtre (S4-J2)

Algorithme : recherche d'un changepoint par z-score et ratio combinés sur
quatre signaux, fenêtre resserrée autour du plus précoce.

**Non mesuré.** Deux obstacles, tous deux liés aux données disponibles :

1. La rétention de Prometheus est de six heures ; les seize incidents de
   la semaine 2 n'ont plus de séries interrogeables. Les points bruts
   sont désormais enregistrés dans `metrics_raw` à la capture, ce qui
   rendra la mesure reproductible — mais seulement pour les captures à
   venir.

2. Sur la capture de test, la baseline ne compte que cinq points avant
   l'injection : le `warmup` de 60 secondes et le redémarrage des
   services ne laissent pas assez de régime sain. La référence du
   détecteur, prise sur huit points, englobe alors l'incident lui-même.

À faire en J6 : captures avec `warmup=180`, puis mesure de l'écart entre
la fenêtre détectée et `injected_at`.

## Détecteur de fenêtre (S4-J2)

Algorithme : recherche d'un changepoint par z-score et ratio combinés sur
quatre signaux, fenêtre resserrée autour du plus précoce, avec une marge
de 60 secondes en amont.

### Mesure

| Capture               | `warmup` | écart au début réel | signaux détectés |
| --------------------- | -------- | ------------------- | ---------------- |
| `INC-20261017-111352` | 60 s     | **+20 s**           | 1                |
| `INC-20261017-114814` | 180 s    | **−39 s**           | 1                |

L'écart négatif est le résultat recherché : la fenêtre commence avant
l'injection, donc avant la cause.

### Le `warmup` conditionne la détection

Avec 60 secondes de trafic sain, la série du pool ne compte que cinq
points avant l'injection. La référence du détecteur, prise sur huit
points, englobe alors l'incident lui-même : la médiane devient la valeur
de panne, et plus aucun point ne s'en écarte. Le détecteur se rabat sur
un signal secondaire — la latence de `payment-service` — et la fenêtre
commence vingt secondes **après** l'injection.

Avec 180 secondes, la baseline compte douze points et le changepoint est
détecté sur le bon signal.

**Conséquence : les captures destinées à mesurer le détecteur doivent
avoir `warmup ≥ 180`.** Les seize captures de la semaine 2 utilisaient
60 secondes et leurs séries ne sont plus interrogeables (rétention
Prometheus de six heures) — elles apparaissent à 0 signal dans
l'évaluation.

### Pourquoi l'écart n'est pas de −60 s

Le détecteur repère l'apparition **métrique** de la panne, pas son
injection. Sur cette capture, le pool passe de 0 à 0,3 à T+6 s et atteint
1,0 à T+21 s : le changepoint est détecté à T+21 s, et la marge de 60 s
ramène le début de fenêtre à T−39 s.

C'est le comportement attendu — un détecteur ne peut pas voir une cause
avant qu'elle ne produise un effet mesurable. La marge existe pour
couvrir ce délai.

## Metrics Agent (S4-J3)

Observation de sept signaux sur la fenêtre délimitée par le détecteur,
comparés à une baseline de cinq minutes.

### Trois modes de comparaison

Un seul critère statistique ne couvre pas tous les signaux.

| Mode         | Signaux                                                  | Critère                      |
| ------------ | -------------------------------------------------------- | ---------------------------- |
| `peak`       | `error_rate`, `dependency_failures`, `p95_by_dependency` | pic de la période            |
| `median`     | `memory_mb`, `cache_entries`, `stock_total`              | changement de niveau         |
| `saturation` | `db_pool_usage`                                          | fraction du temps au plafond |

Le mode `saturation` est né d'un échec de mesure. La gauge du pool est
relevée à chaque requête et vaut 0, 1 ou 2 selon l'instant : son pic
atteint 1,0 en régime sain, et sa variance est telle qu'un pool
_pourtant saturé en permanence_ obtenait un z-score de **2,02** — sous le
seuil de 3,0.

Ni le pic ni la médiane ne distinguent ces deux régimes. Ce qui les
sépare est la proportion de temps passée au plafond : un pool normal y
touche brièvement, un pool fuité n'en redescend plus.

### Résultat sur `INC-20261017-114814-connection_leak`

| Signal                          | Baseline | Incident  | Anomalie |
| ------------------------------- | -------- | --------- | -------- |
| `db_pool_usage`                 | 0,429    | **0,867** | oui      |
| `dependency_failures[postgres]` | 0        | **4,88**  | oui      |
| `error_rate`                    | 0        | **0,962** | oui      |
| `p95[inventory-service]`        | 0,048    | 0,050     | non      |
| `p95[payment-service]`          | 0,085    | 0,091     | non      |
| `memory_mb`                     | 90,6     | 93,5      | non      |

Les trois dernières lignes sont aussi informatives que les trois
premières : l'absence d'anomalie sur les latences et la mémoire écarte
`slow_response`, `cpu_saturation` et `memory_leak`.

### Limite de cette mesure

La baseline affiche 0,429 de saturation, ce qui est élevé pour un régime
sain. Elle recouvre la fin de la capture précédente, le pool n'ayant pas
été libéré entre les deux.

C'est un artefact du protocole de test — captures enchaînées sans
intervalle — et non du comportement en production, où la fenêtre
d'incident est précédée d'heures de trafic normal. L'anomalie reste
détectée malgré cette contamination.

### Latence

| Nœud      | Durée  |
| --------- | ------ |
| `detect`  | 390 ms |
| `metrics` | 438 ms |

Sept requêtes PromQL par nœud, exécutées séquentiellement.


## Log Agent (S4-J4)

Interroge les trois services sur la fenêtre, agrège par type d'événement
et regroupe les lignes non structurées par motif.

### Deux niveaux de lecture

Les logs du bac à sable sont du JSON structuré : le champ `event` suffit
à les agréger. Le template mining (`drain3`) existe pour les logs qu'on
ne contrôle pas — un service tiers, une bibliothèque — où trois cents
messages ne diffèrent que par un identifiant.

Vérifié sur un jeu synthétique : cinq lignes donnent deux motifs
abstraits avec leurs comptes.

Le template doit être relu **après** l'ingestion complète. `drain3`
généralise au fil des messages, et le template retourné sur la première
ligne d'un groupe est encore cette ligne elle-même.

### Résultat sur `INC-20261017-114814-connection_leak` 

checkout-service :
db_pool_timeout ×713 version=v2.7, pool_size=10
dependency_error ×12 dependency=inventory-service, status=409
connection_leaked ×10 version=v2.7, pool_size=10
inventory-service :
out_of_stock ×12


Deux informations que les métriques ne donnent pas :

- le champ `version=v2.7` sur chaque erreur. C'est le seul élément qui
  sépare `connection_leak` de `bad_release`, dont les métriques sont
  identiques.
- la correspondance `dependency_error ×12` / `out_of_stock ×12` entre
  deux services. Le rapprochement se fait par les comptes, sans qu'on
  l'ait programmé.

### Trois listes d'exclusion, trois raisons

| Liste | Sort du résumé | Pourquoi |
| --- | --- | --- |
| `NOMINAL_EVENTS` | oui | leur volume écraserait le signal |
| `SANDBOX_EVENTS` | oui | **ils donnent la réponse** |
| `CONTEXT_EVENTS` | non, marqués `[contexte]` | ils sont la preuve discriminante |

La deuxième ligne est une correction apportée après la première mesure :
`fault_injected` et `fault_cleared` apparaissaient dans le résumé. Ces
événements n'existent que dans le bac à sable, et leur seul nom indique
qu'une panne a été injectée — un Root Cause Agent les lisant n'aurait
plus rien à déduire.

Ils restent dans l'état, consultables pour l'analyse a posteriori. Seul
le résumé transmis au prompt les écarte.

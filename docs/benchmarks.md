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

## Retrieval sur les requêtes du pipeline (S4-J5)

Première mesure du retrieval sur les requêtes que produit réellement
l'Evidence Collector, et non sur les 16 requêtes écrites en S3. Les 19
incidents de `data/incidents/` sont rejoués hors ligne par
`scripts/replay_retrieval.py` : observations reconstruites depuis le JSON,
vrai `build_search_query`, vrai index BM25. Un incident compte comme
trouvé quand un passage du postmortem du bac à sable de son scénario
figure dans les résultats.

### `connection_leak` et `bad_release` ne se séparent pas au retrieval

Sur les 9 incidents de pool (6 `connection_leak`, 3 `bad_release`), on
compare le meilleur passage de chacun des deux postmortems, sous quatre
variantes de la requête :

| Variante de la requête | `bad_release` devant |
| --- | --- |
| telle que construite | 9/9 |
| ligne `deployment` retirée | 9/9 |
| ligne `deployment` forcée, y compris sur les `connection_leak` | 9/9 |
| seules les lignes de checkout-service conservées | 9/9 |

`bad_release` passe devant dans tous les cas, que l'incident soit une
fuite ou un déploiement, et que la requête mentionne un déploiement ou
non. Ses trois premières places sur ses propres incidents ne sont pas une
réussite du retrieval : c'est un biais constant, qui tombe juste une fois
sur trois.

La cause est lexicale. La métrique que le système émet s'appelle
`db_pool_usage`. Elle figure dans les tableaux « Observed symptoms » de
`bad_release`, `internal_error`, `memory_leak` et `cpu_saturation`, mais
nulle part dans `connection_leak.md`, qui écrit « pool gauge ». Sur
`INC-20261017-114814`, `db_pool_usage` et son composant `usag`
fournissent 16,5 % du score du tableau de `bad_release` (22 % avec
l'ancienne requête, qui répétait le terme), et zéro à celui de
`connection_leak`. Aucun terme de la requête ne favorise `connection_leak`
face à `bad_release` : les deux seuls termes propres au pool,
`db_pool_timeout` et `connection_leaked`, figurent dans les deux
documents.

### Pourquoi c'est acceptable

Les deux documents sont dans les cinq premiers résultats sur 9 incidents
sur 9, dans les trois configurations mesurées ci-dessous. Le retrieval
remplit donc son rôle, qui est de fournir les bonnes hypothèses
candidates. Choisir entre elles revient au raisonnement, qui dispose de
l'événement `deployment` dans le résumé. Chaque postmortem dit d'ailleurs
lui-même, dans sa section « Discriminating signal », que c'est cet
événement qui tranche.

Pour ces deux scénarios, le rang 1 n'est donc pas l'objectif du
retrieval. L'objectif est la présence des deux documents dans les cinq
premiers.

### Trois changements, mesurés séparément

1. **Retrait de la répétition.** Le collector doublait les métriques et
   les événements du service en incident pour les faire peser plus que le
   bruit des dépendances. Il ne change l'ordre `connection_leak` /
   `bad_release` sur aucun des 9 incidents de pool. En revanche, il
   doublait `dependency_error`, émis par checkout-service, ce qui faisait
   remonter `internal_error` sur les fuites.
2. **Un passage par document.** `SparseIndex.search_documents`
   sur-récupère puis garde le meilleur chunk de chaque document. Le
   pipeline l'utilise. `search` reste au niveau du chunk, parce que
   `eval/retrieval_eval.py` et `HybridIndex` en dépendent. Les chiffres
   de S3 sont inchangés (16 requêtes incident : recall@5 100 %, MRR
   0,5844).
3. **Section « Discriminating signal » transmise.** Chaque
   `RetrievedPassage` issu d'un postmortem du bac à sable porte cette
   section, recopiée du corpus. Pour les 41 documents publics, le champ
   reste vide. Ce changement n'a aucun effet sur le classement : il
   concerne le raisonnement, pas le retrieval.

| Configuration | rang 1 | top 3 | top 5 | MRR | paire de pool dans le top 5 |
| --- | --- | --- | --- | --- | --- |
| avant (avec répétition) | 11 | 17 | 17 | 0,722 | 9/9 |
| sans répétition | 10 | 13 | 16 | 0,655 | 9/9 |
| sans répétition + un passage par document | 10 | 18 | **19** | 0,706 | 9/9 |

Rangs du bon document, incident par incident :

| Scénario | avec répétition | sans répétition | + un passage par document |
| --- | --- | --- | --- |
| `connection_leak` (×6) | 2, 3, 2, 2, 3, 3 | 2, 5, 2, 2, 5, 5 | 2, 3, 2, 2, 3, 3 |
| `bad_release` (×3) | 1, 1, 1 | 1, 1, 1 | 1, 1, 1 |
| `internal_error` (×3) | 1, 1, 1 | 1, 1, 1 | 1, 1, 1 |
| `slow_response` (×3) | 1, 1, 1 | 1, 1, 1 | 1, 1, 1 |
| `memory_leak` (×2) | 1, 1 | 9, 1 | 3, 1 |
| `cpu_saturation` (×2) | 12, 7 | 12, 7 | 4, 3 |

Retirer la répétition coûte un rang 1 : `INC-20261003-210740-memory_leak`
passe du rang 1 au rang 9. Le passage par document le ramène au rang 3,
mais pas au premier. En contrepartie, la configuration retenue
(dernière colonne) trouve les 19 incidents dans les cinq premiers, sans
manipuler les fréquences de termes.

### Points ouverts

- **`cpu_saturation`.** Au niveau du chunk, le postmortem manque le top 5
  sur ses deux incidents (rangs 12 et 7). Avec un passage par document,
  il y entre aux rangs 4 et 3, uniquement parce que les doublons des
  autres documents ont disparu. Ses passages ne sont pas absents (4 et 5
  dans les vingt premiers chunks), mais tous passent derrière ceux de
  `slow_response` et `internal_error`. Cause non étudiée.
- **Nommage dans `connection_leak.md`.** « pool gauge » au lieu de
  `db_pool_usage`, alors que les cinq autres postmortems emploient le nom
  de la métrique. C'est la cause unique de l'ordre mesuré plus haut. Ce
  point est traité séparément : modifier le corpus pour améliorer une
  mesure demande une justification de cohérence, pas de score. *Corrigé
  ensuite : voir la section suivante.*

### Limites

- **L'échantillon est petit** : 9 incidents de pool, 3 `internal_error`,
  3 `slow_response`, 2 `memory_leak`, 2 `cpu_saturation`. Un écart d'un
  incident vaut 5 points de pourcentage sur l'ensemble, et 50 points sur
  `memory_leak` ou `cpu_saturation`. Les différences de rang 1 entre
  configurations (11 contre 10) tiennent à un seul incident.
- **Les métriques de 16 incidents sur 19 sont approchées.** Seules les
  trois captures du 17 et du 19 octobre ont `metrics_raw`. Pour les
  autres, `compare()` reçoit le pic, la moyenne et la dernière valeur au
  lieu de la série. Sur les trois captures qui ont les deux, l'ensemble
  des métriques anormales est identique, mais trois cas ne prouvent pas
  que l'approximation tient partout.
- **Les logs sont ceux de la phase `incident` stockée**, pas ceux de la
  fenêtre que délimiterait le détecteur.
- **Les 9 incidents de pool portent tous sur checkout-service, avec un
  seul motif de trafic (`diurnal`)**, à une charge de 4 à 9 req/s. Ils ne
  disent rien de la séparabilité sur un autre service ou une autre forme
  de trafic.
- **La ligne « avec répétition » a été mesurée avec une copie de
  l'ancien `build_search_query`**, extérieure au dépôt. Elle a été
  vérifiée identique à l'original sur les 19 requêtes avant le retrait.
  `scripts/replay_retrieval.py` ne mesure que le code actuel.

## Corpus : nommage de `db_pool_usage` dans `connection_leak.md` (S4-J5)

### Ce qui a changé, et pourquoi

Ce changement a été fait **après** le travail sur le retrieval de la
section précédente, et non pendant.

Il est motivé par la cohérence du contenu. Les cinq autres postmortems
nomment la métrique `db_pool_usage` dans leur tableau « Observed
symptoms ». `connection_leak.md` y écrivait « pool gauge ». Une seule
ligne a été modifiée :

    - | pool gauge | 0 | climbs to 1 | **stays at 1** |
    + | `db_pool_usage` | 0 | climbs to 1 | **stays at 1** |

Les valeurs restent celles du document. La prose (« pool gauge at 1 »
dans la chronologie, « how the gauge behaves » dans l'investigation)
n'est pas touchée : aucun des cinq autres documents ne nomme la métrique
dans sa prose — `bad_release.md` écrit lui aussi « the same gauge ». Y
ajouter `db_pool_usage` n'aurait servi qu'à augmenter la fréquence du
terme.

Le lecteur doit savoir que ce changement a été décidé en connaissant son
effet probable : la section précédente montrait que cette ligne était la
cause de l'ordre `bad_release` devant `connection_leak`. La justification
retenue est la cohérence. Les deux séries de chiffres figurent ci-dessous
pour que chacun juge de l'effet.

Contrôles : régénérer `chunks.jsonl` depuis le document d'origine
redonne un fichier identique octet pour octet. Après la modification, un
seul chunk diffère, `sandbox/connection_leak.md#3-0` (122 → 125 tokens).
L'index BM25 a été reconstruit. **L'index dense (Qdrant) ne l'a pas
été** : les mesures dense et hybride de S3 restent celles de l'ancien
corpus.

### Avant / après

Requêtes du pipeline, 19 incidents (`scripts/replay_retrieval.py`), sans
répétition :

| Configuration | Corpus | rang 1 | top 3 | top 5 | MRR | paire de pool dans le top 5 |
| --- | --- | --- | --- | --- | --- | --- |
| par chunk | avant | 10 | 13 | 16 | 0,655 | 9/9 |
| par chunk | après | 13 | 16 | 16 | 0,781 | 9/9 |
| un passage par document | avant | 10 | 18 | 19 | 0,706 | 9/9 |
| un passage par document | après | **13** | 18 | 19 | **0,811** | 9/9 |

Seuls les rangs des incidents `connection_leak` bougent :

| `connection_leak` (×6), un passage par document | rangs |
| --- | --- |
| avant | 2, 3, 2, 2, 3, 3 |
| après | 1, 2, 1, 1, 2, 2 |

Les trois incidents encore au rang 2 ont `internal_error` devant, et non
`bad_release`. Les autres scénarios sont inchangés, `cpu_saturation`
compris (rangs 4 et 3).

Jeux d'évaluation de S3, BM25 seul, k = 5 :

| Jeu | Corpus | recall@5 | MRR |
| --- | --- | --- | --- |
| 16 requêtes incident | avant | 100 % | 0,5844 |
| 16 requêtes incident | après | 100 % | 0,6729 |
| 10 requêtes manuelles | avant | 80 % | 0,7000 |
| 10 requêtes manuelles | après | 80 % | 0,7000 |

Les chiffres de S3 publiés plus haut ont été mesurés sur l'ancien corpus.
Ils ne se comparent pas directement aux mesures faites après ce
changement.

### Ce que cela révise dans la section précédente

La conclusion « `connection_leak` et `bad_release` ne se séparent pas au
retrieval » valait pour l'ancien corpus. Le même test, refait sur le
corpus corrigé, compte le nombre de cas où le document attendu passe
devant l'autre :

| Variante de la requête | avant | après |
| --- | --- | --- |
| telle que construite | 3/9 | **9/9** |
| ligne `deployment` retirée des `bad_release` | `bad_release` devant 3/3 | `connection_leak` devant 2/3 |
| ligne `deployment` forcée sur les `connection_leak` | `bad_release` devant 6/6 | `bad_release` devant 4/6 |

La ligne `deployment` joue désormais le rôle pour lequel elle a été
conçue : elle inverse l'ordre dans 6 des 9 cas contrefactuels, contre 0
avant.

Mais l'écart reste mince. Avec les requêtes telles que construites, le
document attendu devance l'autre de 0,68 à 3,95 points, et de moins d'un
point sur 5 des 9 incidents. Un changement mineur dans la requête ou
dans le corpus peut le renverser.

La décision d'architecture ne change donc pas. Le retrieval garantit la
présence des deux documents (9/9 dans le top 5). Le choix entre eux
revient au raisonnement, à partir de l'événement `deployment` et des
sections « Discriminating signal ». Que l'ordre du retrieval soit
maintenant le bon est un plus, pas une garantie.

### Limites

Les mêmes que dans la section précédente : 9 incidents de pool,
métriques approchées pour 16 incidents sur 19, logs de la phase
`incident` stockée. S'y ajoute un point propre à ce changement : il a
été évalué sur les incidents mêmes qui ont servi à diagnostiquer le
problème. Le jeu marque six incidents `holdout` (un par scénario), mais
le diagnostic comme cette mesure les ont inclus. Ils ne constituent donc
pas une vérification indépendante. Sur ces six seuls, un passage par
document, le seul rang qui change est celui de
`INC-20261003-194922-connection_leak`, qui passe de 2 à 1.


> Mesures effectuées sur le corpus antérieur à S4-J5. Les chiffres
> actualisés figurent dans la section « Retrieval — mesures après
> correction du corpus ».

## Retrieval — mesures après correction du corpus (S4-J5)

Index dense reconstruit, 26 requêtes (16 incident + 10 descriptives).

| Config | recall@5 | MRR | latence |
| --- | --- | --- | --- |
| dense seul | 69,2 % | 0,522 | 110 ms |
| **BM25 seul** | **92,3 %** | **0,683** | **2,6 ms** |
| hybride RRF | 80,8 % | 0,596 | 164 ms |
| hybride + reranking | 73,1 % | 0,576 | 14 803 ms |

| | dense | BM25 | hybride | rerank |
| --- | --- | --- | --- | --- |
| incident (16) | 81 % | **100 %** | 88 % | 69 % |
| descriptif (10) | 50 % | **80 %** | 70 % | 80 % |

Le classement établi en S3 est confirmé sur un corpus corrigé et sur les
deux jeux d'évaluation : BM25 seul domine les trois autres
configurations, sur les deux profils de requête, pour 2,6 ms contre 110
à 14 800 ms.

`memory_leak` passe de 0 % à 100 % sur BM25 — effet de la correction du
nommage, qui rend les tableaux « Observed symptoms » comparables entre
les six postmortems.

Le reranking reste la pire configuration malgré 80 % sur les requêtes
descriptives : il s'effondre sur les requêtes structurées (69 %), pour
un coût de 14,8 secondes par requête.
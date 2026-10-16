# Sondes d'analyse

Scripts de diagnostic jetables, écrits au fil des enquêtes sur la base réelle
pour trancher une question que les tests unitaires ne pouvaient pas : le coût
d'une requête, la limite d'un shell, la sémantique d'un `DBA_VIEW`, la présence
d'un `GRANT`. Ils ne font pas partie du produit ; ils documentent **comment**
une décision a été prise.

## Exécution

Depuis la racine du projet, avec `config/config.conf` renseigné :

```sh
python3 work/probes/probe_grant.py
```

Prérequis : accès à la source et/ou à la cible (réseau, wallet), comme l'outil
lui-même. La plupart sont **en lecture seule** ; les sondes Data Pump et de
transport passent par le runner distant et exécutent des scripts sur les hôtes.

Seuls les `.py` sont versionnés (les `.log`, `.out`, `.err` et `.ok` du dossier
restent ignorés). Toute nouvelle sonde se crée **ici**.

## Filtrage des tables et règle STANDALONE

| Sonde | Objet |
|---|---|
| `probe_plan.py` | Où et comment le plan de filtrage casse (lecture seule) |
| `probe_fix.py` | Vérifie `plan_table_filter` et `schema_segment_bytes` : comptes et durées |
| `probe_add.py` | `EXCLUDE_TABLES` + `EXCLUDE_STANDALONE_TABLES` s'additionnent-ils ? |
| `probe_big.py` | Les plus grosses tables sont-elles dans le jeu STANDALONE ? |
| `probe_lis.py` | `LIS_MERCHANT_DETAIL_TCR_*` : standalone ou non ? |
| `probe_space.py` | Cohérence de l'estimation d'espace avec le plan standalone |
| `probe_standalone_cmp.py` | Requête unifiée vs FK + lecture complète + Python |
| `probe_standalone_deps.py` | Impact du contrôle « aucun autre objet n'utilise » |
| `probe_standalone_ext.py` | Dépendants de nos tables venant d'un **autre** schéma |
| `probe_standalone_own.py` | Usage des tables en ignorant les triggers propres à la table |
| `probe_standalone_sql.py` | Requête unifiée (FK + aucun dépendant non-table), chronométrée |
| `probe_standalone_trig.py` | Trigger propre à la table vs trigger d'une autre table |

## Data Pump : syntaxe et parfile

| Sonde | Objet |
|---|---|
| `probe_dp.py` | R1 (séparateur), R2 (syntaxe `query`) sur la 19c réelle |
| `probe_dp2.py` | R3 isolé : `INCLUDE` seul vs `INCLUDE`+`EXCLUDE` |
| `probe_dp3.py` | Combinaisons `EXCLUDE` + `QUERY` (modes A/B), `INCLUDE` + `QUERY` |
| `probe_dp4.py` | Deux `query=` dans un même parfile (mode B multi-tables) |
| `probe_parfile.py` | Le parfile du mode B (5634 lignes, ~250 Ko) survit-il ? |
| `probe_excl.py` | Parfile produit pour `EXCLUDE_TABLES` + `EXCLUDE_TABLES_DATA` (harnais de test) |
| `probe_full.py` | Run `EXCLUDE_TABLES` via le harnais : succès et nombre de scripts Data Pump |
| `probe_r4.py` | R4 : fermeture des dépendances dans `DBA_OBJECTS` |
| `probe_r4b.py` | R4 (suite) : comment `DBA_OBJECTS` nomme les LOB et les partitions |
| `probe_sql.py` | Structure de `DBA_SEGMENTS`, fermeture des dépendances, liste des tables |

## Limites du shell distant et transport

| Sonde | Objet |
|---|---|
| `probe_limit.py` | Taille maximale d'une requête acceptée par le shell distant |
| `probe_limit2.py` | Seuil exact, code d'erreur, notation scientifique |
| `probe_lines.py` | La limite est-elle la ligne ou le volume total ? |
| `probe_rc5.py` | Un script qui sort en 5 doit **délivrer** son bloc (Ansible/sshpass) |

## Dictionnaire et performances

| Sonde | Objet |
|---|---|
| `probe_counts.py` | Volumétrie des vues dictionnaire par schéma |
| `probe_deps.py` | Coût de la lecture des dépendances intra-schéma |
| `probe_pull.py` | Coût des lectures « dictionnaire entier » (sans liste) |
| `probe_sum.py` | Durée de `schema_segment_bytes` sans filtre |

## Privilèges et grants

| Sonde | Objet |
|---|---|
| `probe_grant.py` | La création du schéma cible reconduit-elle les `GRANT` système ? |
| `probe_grant_cmp.py` | Compare les `GRANT` système entre le compte source et le compte cible |
| `probe_tabprivs.py` | Quels `GRANT` d'objet le compte source a-t-il reçus / donnés ? |
| `probe_sysgrants.py` | Détail des 22 grants d'objet SYS reçus, et leur impact (PUBLIC ou manquant) |

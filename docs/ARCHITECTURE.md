# Architecture

## Principe directeur

**Intelligent local, bête distant.**

L'intelligence — validation, décisions, contrôle de succès, réconciliation —
est sur le **serveur de saut**, en Python. L'hôte AIX qui porte la base ne
receit qu'un script `sh` qui produit un résultat analysable. Il n'a ni Python,
ni logique, ni état.

La conséquence est directe : le serveur de saut **n'a pas de client Oracle**,
et n'en a pas besoin. Il ne lit pas la base, il ne connaît pas le schéma ; il
décide et compare. Tout ce qui exige le client s'exécute chez la base.

## Les 19 étapes

`src/osd/stages/pipeline.py` définit l'ordre canonique, utilisé tel quel par
`run`, `resume` et `check` :

| # | Étape | Ce qu'elle fait |
|---|-------|-----------------|
| 1 | charger-configuration | lit le fichier, résout les répertoires |
| 2 | valider-configuration | schéma de config, cohérence, garde-fous |
| 3 | verifier-dependances | `expdp`/`impdp`/`sqlplus` des deux côtés |
| 4 | tester-connexion-source | version et instance réellement ouvertes |
| 5 | tester-connexion-cible | idem côté cible |
| 6 | verifier-schema-source | inventaire du schéma source |
| 7 | verifier-schema-cible | existence et peuplement de la cible |
| 8 | verifier-tablespaces | DIRECTORY, tablespaces, privilèges |
| 9 | verifier-espace | disque du DIRECTORY et tablespaces de la cible |
| 10 | preparer-datapump | noms de job, de dump, de journaux |
| 11 | exporter | `expdp` + énumération des parties réelles |
| 12 | verifier-dump | relecture `SQLFILE`, sans écrire en base |
| 13 | transferer | copie vers la cible, si les côtés ne partagent pas le répertoire |
| 14 | importer | `impdp` avec remap |
| 15 | valider | inventaire, objets invalides, volumétrie |
| 16 | comparer | réconciliation source / cible |
| 17 | generer-rapport | état écrit sur disque |
| 18 | nettoyer | suppression des artefacts |
| 19 | retourner-code | fixe le code de sortie |

`run` les enchaîne toutes. `check` s'arrête à 9. `resume` saute celles déjà
validées.

## Composants

```
bin/osd                     lanceur : localise le code, exec python3
src/osd/
  cli.py                    arguments, dispatch, orchestration, signaux
  config.py                 schéma typé, validation, valeurs par défaut
  errors.py                 OsdError et sa hiérarchie (code de sortie)
  exit_codes.py             table 0..9, libellés
  redact.py                 masquage des secrets, partout
  logging_setup.py          journalisation, un fichier par run
  lock.py                   exclusion mutuelle entre runs
  state.py                  état des 19 étapes, persistance, reprise
  runner.py                 LocalRunner / RemoteRunner, exécution
  adapters/
    oracle.py               connexion, SQL*Plus, DIRECTORY, requêtes
    datapump.py             expdp/impdp, parfiles, état des jobs
    transfer.py             choix et exécution du backend de transfert
    null.py                 NullRunner — la simulation
  checks/preflight.py       les contrôles des étapes 3 à 9
  stages/pipeline.py        l'enchaînement des 19 étapes
  report/                   construction du document, rendu texte
shell/                      scripts POSIX sh exécutés chez la base
  prelude.sh                contrat d'exécution : set -u, trap, canal machine
  remote_*.sh               un fichier par famille d'opérations
```

## Les trois décisions qui structurent le reste

### 1. La simulation est une substitution, pas une condition

`--dry-run` ne sprinkle pas de `if self.dry_run` dans le code métier. Il
remplace le runner par un `NullRunner` qui **enregistre** les appels sans les
exécuter, et répond comme le ferait un hôte. Le chemin de code parcouru est
donc exactement le chemin réel : rien ne peut y diverger.

Ce qui découle :

- une étape qui oublierait de se déclarer mutante s'exécuterait pour de vrai
  en simulation. D'où `mutating=True` sur l'export, l'import et le nettoyage,
  et le test qui vérifie qu'un `--dry-run` ne produit **aucune** mutation réelle ;
- le `NullRunner` doit répondre aussi fidèlement qu'un hôte (`Result`, lignes,
  `kv`), sinon la vérificationavant-jumporterait sur des données inventées.

### 2. Le succès Data Pump ne se lit pas dans le texte des messages

Un succès est établi par **trois** conditions, toutes nécessaires :

1. le code de retour est dans `DATAPUMP_SUCCESS_CODES = {0, 1, 2, 4, 8}` ;
2. aucun code `ORA-`/`UDI-`/`DBMGSPC-` dans la sortie ;
3. le job est à l'état `COMPLETED` dans `DBA_DATAPUMP_JOBS`, **sur la base où
   il s'est déroulé**.

Chacune manque quelque chose. Un rc à 0 n'exclut pas une erreur Oracle dans une
section non fatale. L'absence d'erreur n'exclut pas un job arrêté. Et le client
peut se détacher (`EXIT_CLIENT`) en laissant tourner le job : le rc ne dit alors
rien du résultat.

Aucune recherche de « successfully completed » : le message est traduit, la
recherche serait au mieux fragile et au pire fausse.

`DATAPUMP_OK_STATES` ne contient **que** `COMPLETED`. `RUNNING`, `FAILED`,
`STOPPED`, `NEEDS_COMMIT` sont des échecs — valider un dump encore en écriture
serait pire que de ne rien valider.

L'état prime sur `ERROR_COUNT`, et non l'inverse : un compteur n'a de sens que
si le job a abouti, et la colonne est absente de certaines vues 19c réduites.
L'inverse est le piège — un job `FAILED` au compteur illisible passerait pour un
succès.

### 3. Le nettoyage est centralisé dans le prelude

Chaque fichier temporaire distant est déclaré supprimable **avant** d'être
rendu, et le `trap` du prelude supprime la liste en sortie. Un script qui meurt
entre les deux ne laisse donc pas de fichier contenant un `userid` — donc un
secret — lisible par tout compte de l'hôte.

C'est aussi pourquoi `osd_register_cleanup` précède *tout* `osd_die` : le
parfile Data Pump porte la chaîne de connexion, il ne doit pas survivre au
traitement.

## Le protocole distant

Détail dans `docs/REMOTE_PROTOCOL.md`. En résumé :

- le script complet part sur `stdin` : `ssh -o BatchMode=yes <hôte> sh -s` ;
- assemblage dans cet ordre : environnement → prelude → amorçage → arguments →
  corps. L'ordre est significatif, et les deux inversions possibles sont
  documentées dans `build_script` ;
- `stdout` ne contient **que** du protocole. Le prelude redirige `stdout` vers
  `stderr` et conserve le vrai `stdout` sur le descripteur 3 : tout ce qui sort
  entre `OSD_ROWS_BEGIN` et `OSD_ROWS_END` doit être écrit `>&3` ;
- `stderr` reçoit les diagnostics, donc la sortie de l'outil Oracle, traduite
  dans la locale de l'hôte — et donc inutilisable telle quelle.

`stdout` du processus `osd` lui-même porte **uniquement le rapport** ; les
journaux vont sur `stderr`. Un ordonnanceur peut donc capturer le rapport sans y
trouver de trace de diagnostic.

## La topologie de transfert

`method` est la **topologie**, pas un détail technique :

| Topologie | Condition | `method` | `backend` |
|-----------|-----------|----------|-----------|
| Les deux côtés voient le même DIRECTORY | deux `LocalRunner` | `partage` | `local` |
| Au moins un côté est distant | — | `relais` | `scp`/`rsync`/`sftp` |
| Un côté local, un côté distant | — | refusée | — |

La topologie mixte est **refusée**, et non traitée comme `partage` : c'est la
seule des trois dont le transfert n'a pas de forme connue. Le nommer permet de
la désigner dans le message d'erreur.

`partage` n'est pas un mécanisme de copie mais l'absence de copie, et c'est
`TransferBackend.run()` qui s'en charge — pas chaque appelant. Lire le mode
*configuré* ne suffisait pas : `TRANSFER_MODE` absent ne dit rien de la
topologie, et le run échouait en cherchant un `scp` pour un fichier déjà en
place.

`AUTO` sonde réellement les backends (écriture + relecture + suppression d'un
fichier d'échantillon minuscule) plutôt que de se fier à la présence du binaire :
un `scp` installé peut ne pas fonctionner si le `sshd` n'expose pas `sftp`.

## Secrets

- `redact.py` est appelé par le formateur de logs, le rendu du rapport, la
  sérialisation de l'état et l'affichage des commandes en simulation ;
- le mot de passe n'apparaît **jamais** dans les arguments de processus : il est
  dans le parfile, écrit en `0600`, supprimé en sortie par le prelude ;
- wallet privilégié. `PASSWORD` est un repli, et l'extrait ci-dessous ;
- `expdp`/`impdp` refusent `AS_SYSDBA` sur la ligne de commande (`LRM-00112`) :
  l'option ne passe donc jamais par `argv`, seulement par le parfile.

## Exclusion obligatoire à l'import

`EXCLUDE_A_IMPORT = ("USER",)` est posé par l'outil et non par la
configuration, et fusionné avec les exclusions du fichier sans qu'aucune
puisse le dissoudre.

Un export de schéma embarque l'objet `USER` du schéma exporté, que
`REMAP_SCHEMA` renomme à l'import : l'import cherchait donc à **créer le compte
cible**, qui existe par construction — l'étape 7 exige qu'il existe. L'échec
était `ORA-31684`, *après* avoir chargé toutes les données.

## Exclusion d'objets au nettoyage

`TABLE_EXISTS_ACTION` ne couvre que les **tables**. C'est une limite de Data
Pump, pas de l'outil, et elle est nommée dans le remède de `ORA-31684` : un
schéma cible partiellement peuplé échoue sur ses séquences, procédures,
index, triggers et vues, quel que soit le `TABLE_EXISTS_ACTION` choisi.

# Exigences

Ce document énonce ce que l'outil fait, et **pourquoi** lorsque la contrainte
n'est pas évidente. Les choix d'implémentation sont dans
`docs/ARCHITECTURE.md`, l'exploitation dans `docs/OPERATIONS.md`, les pannes dans
`docs/RUNBOOK.md`.

## Fonction principale

Dupliquer un schéma Oracle 19c d'une base source vers une base cible, par Data
Pump, piloté depuis un serveur de saut.

## Décision d'exécution

**Serveur de saut Linux, Python 3.9+, hôtes AIX en SSH. Aucun client Oracle sur
le serveur de saut.**

« Intelligent local, bête distant » : la validation, les décisions, le contrôle
de succès et la réconciliation sont sur le serveur de saut ; l'hôte AIX ne
receit qu'un script `sh` dont il faut lire un résultat.

Ce choix a un coût — un serveur de saut à maintenir — et un bénéfice décisif :
le serveur de saut n'a pas de version de client Oracle à suivre, et il ne
devient pas un second point d'échec Oracle à diagnostiquer.

## Connexion

Supporté :

- **EZCONNECT** — `host:1521/SERVICE`, sans `tnsnames.ora`.
- **alias TNS** — `SOURCE_TNS_ADMIN` / `TARGET_TNS_ADMIN` sont transmis à
  l'hôte. Le `TNS_ADMIN` du serveur de saut ne sert à rien ; son oubli produit
  un `ORA-12154` que rien dans le rapport ne rattache à sa cause.
- **RAC / SCAN** — DESCRIPTION complète, comme n'importe quel alias.
- `service_name` et port configurables.
- `SOURCE_SYSDBA` / `TARGET_SYSDBA`, pour les cas où l'administrateur l'exige.

`BatchMode=yes` est **exigé**, pas recommandé : sans lui une erreur
d'authentification ouvre une invite et le run reste bloqué jusqu'à l'expiration
du crontab.

## Export

`expdp`, avec : `SCHEMAS`, `DIRECTORY`, `DUMPFILE`, `LOGBFILE`, `PARALLEL`,
`COMPRESSION`, `EXCLUDE`, `INCLUDE`, `CONTENT`, `FILESIZE`.

`DUMPFILE` en écriture utilise `%d` quand `PARALLEL > 1`. **L'import, lui,
reçoit la liste concrète des parties** (`_spec_dimport`), énumérée à l'étape
11 : `%d` n'est pas accepté en lecture, et l'utiliser produit `ORA-39124`.

`SQLFILE` n'est pas un substitut de `DUMPFILE` pour la relecture : c'est un
paramètre d'import qui produit le DDL, et `TABLE_EXISTS_ACTION` est incompatible
avec lui (`ORA-39208`).

## Import

`impdp`, avec :

- `REMAP_SCHEMA` — **toujours** appliqué, jamais désactivable.
- `REMAP_TABLESPACE` — paires `SRC:DST`, validées à la lecture.
- `TABLE_EXISTS_ACTION` — `SKIP`, `APPEND`, `REPLACE`, `TRUNCATE`. Les deux
  derniers exigent `--allow-destructive`, revérifié à l'étape 14.
- `PARALLEL`, `EXCLUDE`, `INCLUDE`, `SQLFILE`.
- `EXCLUDE=USER` — posé **par l'outil**, fusionné avec les exclusions de la
  configuration, non surchargeable.

### Pourquoi `EXCLUDE=USER` est imposé

Un export de schéma embarque l'objet `USER` du schéma exporté, que
`REMAP_SCHEMA` renomme à l'import. L'import cherchait donc à **créer le compte
cible**, qui existe par construction — l'étape 7 exige qu'il existe.

L'échec (`ORA-31684`) survenait **après** avoir chargé toutes les données : le
run se concluait sur une erreur alors que la duplication était complète, et il
fallait songer à la main à chaque fois.

L'exclusion est posée **aussi en mode `SQLFILE`** (étape 12) : elle porte sur
le jeu d'objets lus, donc la relecture doit décrire exactement l'import annoncé.
Une relecture décrivant un import plus large que l'import réel
ne verrait pas le défaut qu'elle est censée détecter.

## Précontrôles

| Étape | Contrôle |
|-------|----------|
| 3 | `expdp`, `impdp`, `sqlplus` des deux côtés |
| 4 | connexion source : version et instance réellement ouvertes |
| 5 | connexion cible : idem |
| 6 | schéma source : inventaire |
| 7 | schéma cible : existence et peuplement |
| 8 | `DIRECTORY`, tablespaces, privilèges (`SESSION_PRIVS`) |
| 9 | place du `DIRECTORY` source et des tablespaces cibles |

Les étapes 4 à 9 s'exécutent **réellement** en mode `--dry-run` : elles ne
sont pas simulées. Un dry-run qui ne se connecterait pas ne prouverait rien.

L'estimation d'espace est une **borne basse** : elle mesure les segments du
schéma source, pas les index, LOB et fragmentation. Un refus n'est pas une
garantie d'échec, un succès n'est pas une garantie de place.

## Validation

Après import : inventaire d'objets, tables, index, contraintes, séquences,
triggers, vues, grants, synonyms, **objets invalides**, volumétrie, puis
réconciliation source/cible objet par objet.

Un objet invalide n'est pas un détail : c'est un schéma qui ne compile pas côté
cible. `STANDARD` échoue lui aussi, ce qui est normal et attendu.

## Sécurité

- **Aucun mot de passe en clair** : ni dans le code, ni dans les logs, ni dans
  les fichiers versionnés, ni dans les **arguments de processus**.
- Wallet préféré, en deux variantes (externe, ou `DIRECTORY` wallet interne).
  `PASSWORD` est un repli, masqué partout.
- Jamais d'`eval`, nulle part, quelle que soit la forme des arguments.
- Jamais d'opération destructive sans option explicite : `--allow-destructive`,
  vérifié à l'étape 2 **et** revérifié à l'étape 14, parce qu'un `resume` peut
  contourner l'étape 2.
- Le mot de passe n'est jamais dans les arguments de processus : il est dans le
  parfile, écrit en `0600`, supprimé en sortie par le prelude. `expdp`/`impdp`
  refusent `AS_SYSDBA` sur la ligne de commande (`LRM-00112`) : l'option ne
  passe donc jamais par `argv`.

## Retour et rapport

- **Codes 0 à 9**, table unique de vérité (`src/osd/exit_codes.py`). Toute
  erreur se mappe sur **exactement un** code. `main()` ne lève jamais et ne
  rend **jamais** `None`.
- `stdout` ne porte **que** le rapport ; les journaux vont sur `stderr`. Un
  ordonnanceur capture donc le rapport sans y trouver de diagnostic.
- Rapport **JSON versionné**, sans secret, avec `carried_over` par étape.
- `stdout`/`stderr` du protocole distant : voir `docs/REMOTE_PROTOCOL.md`.

## Interruption et reprise

- `SIGINT` / `SIGTERM` → arrêt propre, code **9**, état écrit. Second signal →
  `os._exit` immédiat, sans état à moitié écrit.
- Le dump est **conservé** après un échec : c'est la seule voie de recovery.
- `resume` ne rejoue que ce qui n'a pas été validé, et n'a donc pas à refaire
  l'export.
- Après une reprise **réussie**, le nettoyage a lieu comme après n'importe quel
  succès. Le test est « cette invocation a-t-elle échoué ? », pas « un échec a-t-
  il jamais eu lieu ? ».

## Transfert

Trois topologies, déduites de la **réalité** et non du mode configuré :

| Topologie | `method` |
|-----------|----------|
| les deux côtés voient le même `DIRECTORY` | `partage` (aucune copie) |
| au moins un côté distant | `relais` |
| un côté local, un côté distant | **refusée** (`ConfigError` nommant les deux clés) |

Le mode configuré ne dit pas la topologie : `TRANSFER_MODE` absent ne permet
pas de la déduire. C'est `TransferBackend.run()` qui décide, et il mesure tout
de même les parties — le rapport compare ce volume à la place disponible.

`AUTO` sonde les backends **réellement** (écriture, relecture, suppression d'un
fichier d'échantillon) au lieu de se fier à la présence du binaire : un `scp`
installé peut ne pas fonctionner si le `sshd` n'expose pas le sous-système
`sftp`, ce qui est le cas le plus courant sur AIX. La sonde expire en une heure
— un `sshd` reconfiguré entre deux runs ne doit pas produire un échec
inexpliqué.

## Succès Data Pump

Un succès exige **les trois** conditions :

1. code de retour dans `DATAPUMP_SUCCESS_CODES = {0, 1, 2, 4, 8}` ;
2. aucun code `ORA-`/`UDI-`/`DBMGSPC-` dans la sortie ;
3. job à l'état `COMPLETED` dans `DBA_DATAPUMP_JOBS`, **sur la base où il s'est
   déroulé**.

Chacune manque quelque chose : un rc à 0 n'exclut pas une erreur dans une
section non fatale ; l'absence d'erreur n'exclut pas un job arrêté ; et le
client peut se détacher (`EXIT_CLIENT`) en laissant tourner le job, auquel cas
le rc ne dit rien du résultat.

`DATAPUMP_OK_STATES` ne contient **que** `COMPLETED` : `RUNNING`, `FAILED`,
`STOPPED`, `NEEDS_COMMIT` sont des échecs. Valider un dump encore en écriture
serait pire que de ne rien valider.

L'état prime sur `ERROR_COUNT` : un compteur n'a de sens que si le job a
abouti, et la colonne est absente de certaines vues 19c réduites. L'inverse est
le piège — un job `FAILED` au compteur illisible passerait pour un succès.

Aucune recherche de « successfully completed » : le message est traduit, la
recherche serait au mieux fragile et au pire fausse.

## Mode dry-run

Le runner est **substitué** par un `NullRunner`, pas contourné par des `if`
dispersés. Le chemin de code parcouru est donc exactement le chemin réel, et
rien ne peut y diverger.

Ce qui découle :

- une étape oubliant de se déclarer mutante s'exécuterait pour de vrai en
  simulation. D'où `mutating=True` sur l'export, l'import et le nettoyage — et
  le test qui vérifie qu'un dry-run ne produit **aucune** mutation réelle ;
- le `NullRunner` doit répondre aussi fidèlement qu'un hôte, sinon la
  vérification avant-jumporterait sur des données inventées.

## Qualité

Code **modulaire**, **documenté**, **testable**, **robuste**, **exploitable en
production**, **compatible cron**, **correctement journalisé**. Scripts distants
**POSIX sh**, et le Bourne shell d'AIX avec lui — c'est `/bin/sh` sur la
plateforme cible, pas ksh93 : zéro bashisme, zéro Python, zéro `eval`, zéro
`grep -o`, zéro `grep -P`, zéro `sed -i`, zéro `cpio`. Deux particularités de ce
shell sont traitées explicitement, parce qu'elles ne se devinent pas : `set -u`
y est inutilisable, et le trap de sortie n'y reçoit pas le code de sortie. Voir
`docs/REMOTE_PROTOCOL.md`.

## Tests

14 scénarios obligatoires, chacun testable **sans base et sans réseau** :

connexion source indisponible · connexion cible indisponible · schéma inexistant
· tablespace inexistant · espace insuffisant · export échoué · transfert échoué ·
import échoué · objet existant · privilèges insuffisants · erreur Oracle ·
interruption · reprise · concurrence · dry-run.

`unittest` de la bibliothèque standard, sans dépendance externe :

```sh
python3 -m unittest discover -s tests/unit -t tests/unit
```

Les tests sontpreferably des **executions** que des inspections de source : un
test qui lit le code teste le code, un test qui l'exécute teste le
comportement. Aucun test de verrou ne tient le verrou dans le processus de test
— `fcntl` appartient au processus, il faut donc un vrai fils.

# Oracle Schema Duplicator

Duplication d'un schéma Oracle 19c d'une base source vers une base cible, par
Data Pump, pilotée depuis un **serveur de saut** Linux.

## Où ça tourne

```
  serveur de saut (Linux)                 base source (AIX)        base cible (AIX)
  ┌───────────────────────┐               ┌──────────────┐        ┌──────────────┐
  │ python3  bin/osd      │  ssh          │ expdp        │        │ impdp        │
  │ (aucun client Oracle) │ ───────────►  │ DIRECTORY    │        │ DIRECTORY    │
  │                       │ ◄───────────  │ tablespaces  │        │ tablespaces  │
  │ 19 etapes             │   résultats   │ privileges   │        │ privileges   │
  │ rapport + code 0..9   │               └──────────────┘        └──────────────┘
  └───────────────────────┘                     dump  ──────────────►
```

Le serveur de saut **n'a pas de client Oracle** et n'en a pas besoin : il
décide et compare, il ne lit pas la base. Tout ce qui exige le client
s'exécute chez la base, par `ssh`.

## Démarrage

```sh
cp config/config.example.conf config/config.conf
$EDITOR config/config.conf

./bin/osd check -c config/config.conf    # etapes 1 a 9, n'ecrit rien
./bin/osd run   -c config/config.conf    # le run complet
```

Le schéma de configuration s'affiche par `./bin/osd config`.

## Le run en une ligne

```
19 etapes : charger-configuration  valider-configuration  verifier-dependances
           tester-connexion-source  tester-connexion-cible  verifier-schema-source
           verifier-schema-cible  verifier-tablespaces  verifier-espace
           preparer-datapump  exporter  verifier-dump  transferer  importer
           valider  comparer  generer-rapport  nettoyer  retourner-code
```

Chaque étape est enregistrée, datée, et interruptible. Le rapport dit ce qui a
été fait, ce qui a échoué, et quoi faire ensuite.

## Commandes

| Commande | Effet |
|----------|-------|
| `osd run` | le run complet |
| `osd check` | étapes 1 à 9 : configuration et prérequis, rien n'est écrit |
| `osd resume` | reprend un run interrompu, sans refaire l'export |
| `osd status` | affiche un run, ou `--all` |
| `osd clean` | supprime états et journaux (destructif) |
| `osd config` | schéma de configuration, ou détail d'une clé |

Codes de sortie : **0** succès, **1** configuration, **2** prérequis,
**3** connexion, **4** export, **5** transfert, **6** import, **7** validation,
**8** sécurité, **9** interruption.

## Ce que l'outil garantit

- **Rien n'est déclaré réussi sur la foi d'un message.** Un succès Data Pump
  exige trois conditions : code de retour conforme, aucun code `ORA-`/`UDI-`,
  et job à l'état `COMPLETED` dans `DBA_DATAPUMP_JOBS` — sur la base où il s'est
  déroulé. Aucune recherche de « successfully completed », qui serait traduite
  donc fausse.
- **Le dump exporté est relu avant d'être importé.** L'étape 12 le relit avec
  `SQLFILE=`, sans écrire en base : un dump tronqué échoue explicitement, plutôt
  que de se découvrir à l'import.
- **Un dry-run est le vrai code.** Le runner est remplacé, pas contourné par des
  `if` dispersés. Les contrôles de connexion, de schéma, de privilège et
  d'espace sont réellement exécutés ; l'export, l'import et le nettoyage ne le
  sont pas, et sont listés comme tels.
- **Aucune opération destructive sans option explicite**, revérifiée juste avant
  l'écriture, parce qu'une reprise peut contourner la validation initiale.
- **Aucun mot de passe** dans un journal, un rapport, un état ou un argument de
  processus. Wallet préféré.
- **Une interruption ne perd rien** : l'état est écrit, le dump est conservé,
  et la reprise ne refait pas l'export.

## Tests

```sh
python3 -m unittest discover -s tests/unit -t tests/unit
```

Bibliothèque standard, aucune dépendance externe, aucun accès réseau ni base
requis. Les 14 scénarios obligatoires sont couverts, chacun exécutable seul.

## Documentation

| Document | Contenu |
|----------|---------|
| [AGENTS.md](AGENTS.md) | méthode de travail, cadre |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | ce que ça fait, et pourquoi |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | structure, décisions structurantes |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | configuration, exécution, cron |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | diagnostic par code de sortie |
| [docs/EXIT_CODES.md](docs/EXIT_CODES.md) | table des codes |
| [docs/REMOTE_PROTOCOL.md](docs/REMOTE_PROTOCOL.md) | contrat d'exécution distante |

#!/usr/bin/env python3
"""Amorce du projet — NE PAS REJOUER.

Ce script a **genere** ce depot a son etat initial. Il est conserve
comme trace de l'amorcage, pas comme outil : son contenu ne decrit plus
le projet.

Deux divergences, qui expliquent qu'il soit dangereux a relancer :

* il ecrit encore le design **Bash** (`src/main.sh`, `src/oracle.sh`,
  ...), abandonne au profit de Python (`bin/osd`, `src/osd/`). Ces
  stubs ont ete supprimes du depot ; le script les recreerait ;
* il embarque une **copie** de `AGENTS.md` qui date de l'amorcage. Toute
  modification du vrai `AGENTS.md` depuis lors existe en deux endroit,
  et la copie est la version perimee.

Rejouer ecraserait `AGENTS.md` et `README.md` par leur texte d'origine.
Les retirer, plutot que les rafraichir : un generateur qu'on remet a
jour est un generateur qu'on oublie de mettre a jour.

Il n'est reference par aucun code du projet, et aucun test ne l'exerce.
"""

from pathlib import Path
import argparse
import sys


FILES = {
    "AGENTS.md": """# Oracle Schema Duplicator

## Mission

Développer un outil industriel permettant de dupliquer un schéma Oracle 19c
d'une base source vers une base cible.

## Environnement

- Linux RHEL 8/9 ou équivalent
- Oracle Database 19c
- Oracle Client 19c
- Bash 4+
- Python 3.9+
- SQL*Plus
- expdp
- impdp
- SSH/SCP
- Oracle Wallet recommandé

## Fonctionnalités

L'outil doit supporter :

- duplication de schéma Oracle
- connexion EZCONNECT
- connexion TNS Alias
- RAC / SCAN
- ASM
- Data Pump
- REMAP_SCHEMA
- REMAP_TABLESPACE
- TABLE_EXISTS_ACTION
- mode dry-run
- validation avant traitement
- validation après traitement
- transfert local ou distant
- journalisation
- gestion des erreurs
- reprise contrôlée
- nettoyage
- reporting
- exécution cron/ordonnanceur

## Workflow

1. Charger la configuration
2. Valider la configuration
3. Vérifier les dépendances
4. Tester la connexion source
5. Tester la connexion cible
6. Vérifier le schéma source
7. Vérifier le schéma cible
8. Vérifier les tablespaces
9. Vérifier l'espace disponible
10. Préparer Data Pump
11. Exporter
12. Vérifier le dump
13. Transférer si nécessaire
14. Importer
15. Valider
16. Comparer source et cible
17. Générer le rapport
18. Nettoyer
19. Retourner le code d'exécution

## Sécurité

- Aucun mot de passe en clair dans le code.
- Aucun mot de passe dans les logs.
- Aucun secret dans Git.
- Utiliser Oracle Wallet lorsque possible.
- Ne jamais utiliser eval.
- Ne jamais effectuer une opération destructive sans option explicite.

## Qualité

Le code doit être :

- modulaire
- documenté
- testable
- robuste
- compatible ShellCheck
- exploitable en production
- compatible cron
- correctement journalisé

## Méthode OpenCode

Avant de coder :

1. Analyser les exigences.
2. Identifier les ambiguïtés.
3. Proposer l'architecture.
4. Identifier les risques.
5. Définir les tests.
6. Implémenter progressivement.
7. Tester.
8. Documenter.

## Tests obligatoires

Tester notamment :

- connexion source indisponible
- connexion cible indisponible
- schéma inexistant
- tablespace inexistant
- espace insuffisant
- export échoué
- transfert échoué
- import échoué
- objet existant
- privilèges insuffisants
- erreur Oracle
- interruption
- reprise
- concurrence
- dry-run
""",

    "README.md": """# Oracle Schema Duplicator

Outil d'automatisation de duplication de schémas Oracle 19c.

## Objectif

Automatiser :

Source Oracle
    |
    | expdp
    v
Dump Data Pump
    |
    | SCP/SFTP
    v
Target Oracle
    |
    | impdp
    v
Schema cible

## Documentation

- AGENTS.md
- docs/REQUIREMENTS.md
- docs/ARCHITECTURE.md
- docs/OPERATIONS.md
""",

    "docs/REQUIREMENTS.md": """# Requirements

## Fonction principale

Dupliquer un schéma Oracle 19c d'une base source vers une base cible.

## Connexion

Supporter :

- EZCONNECT
- TNS Alias
- RAC / SCAN
- service_name
- port configurable

## Export

Utiliser expdp.

Paramètres :

- schema
- directory
- dumpfile
- logfile
- parallel
- compression
- exclude
- include

## Import

Utiliser impdp.

Supporter :

- REMAP_SCHEMA
- REMAP_TABLESPACE
- TABLE_EXISTS_ACTION
- PARALLEL
- EXCLUDE
- INCLUDE
- SQLFILE

## Précontrôles

Vérifier :

- commandes Oracle
- connexion source
- connexion cible
- existence du schéma
- tablespaces
- espace disponible
- DIRECTORY Oracle
- privilèges

## Validation

Après import :

- objets
- tables
- indexes
- contraintes
- séquences
- triggers
- grants
- synonyms
- objets invalides
- erreurs Oracle

## Sécurité

Privilégier Oracle Wallet.

Les mots de passe ne doivent jamais apparaître dans :

- fichiers de configuration versionnés
- logs
- arguments de processus
- code source

## Codes retour

0 = succès
1 = configuration
2 = prérequis
3 = connexion
4 = export
5 = transfert
6 = import
7 = validation
8 = sécurité
9 = interruption
""",

    "docs/ARCHITECTURE.md": """# Architecture

## Composants

### main

Point d'entrée.

### configuration

Chargement et validation de la configuration.

### oracle

Gestion des connexions Oracle et SQL*Plus.

### datapump

Gestion de expdp et impdp.

### transfer

Gestion des transferts de dumps.

### validation

Contrôle source/cible.

### logging

Gestion centralisée des logs.

### reporting

Production du rapport final.

## Principes

- séparation des responsabilités
- modularité
- sécurité
- testabilité
- gestion centralisée des erreurs
- configuration externe
""",

    "docs/OPERATIONS.md": """# Operations

## Prérequis

- Linux
- Bash 4+
- Python 3.9+
- Oracle Client 19c
- SQL*Plus
- expdp
- impdp
- SSH
- SCP

## Configuration

Copier :

config/config.example.conf

vers :

config/config.conf

Ne jamais versionner les secrets.

## Exécution

./src/main.sh --config config/config.conf

## Dry Run

./src/main.sh --config config/config.conf --dry-run

## Logs

Les logs sont stockés dans logs/.

## Exploitation

Le programme doit pouvoir être lancé :

- manuellement
- par cron
- par ordonnanceur d'entreprise
""",

    "config/config.example.conf": """# Oracle Schema Duplicator

CONNECTION_MODE=EZCONNECT

SOURCE_CONNECT="//source-db:1521/ORCLPDB1"
SOURCE_SCHEMA=SOURCE_SCHEMA
SOURCE_DIRECTORY=DP_SOURCE

TARGET_CONNECT="//target-db:1521/ORCLPDB1"
TARGET_SCHEMA=TARGET_SCHEMA
TARGET_DIRECTORY=DP_TARGET

PARALLEL=4
COMPRESSION=ALL

VALIDATION_LEVEL=STANDARD

REMOTE_MODE=false

TARGET_SSH_USER=oracle
TARGET_SSH_HOST=target-db

REMAP_SCHEMA=true
REMAP_TABLESPACE=

TABLE_EXISTS_ACTION=SKIP

DRY_RUN=false
CLEANUP_AFTER_SUCCESS=true

LOG_DIR=logs
WORK_DIR=work
LOCK_FILE=/tmp/oracle-schema-duplicator.lock
""",

    "src/main.sh": """#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Oracle Schema Duplicator"
echo "Implementation pending"
""",

    "src/oracle.sh": """#!/usr/bin/env bash

# Oracle functions
""",

    "src/export.sh": """#!/usr/bin/env bash

# Data Pump export functions
""",

    "src/import.sh": """#!/usr/bin/env bash

# Data Pump import functions
""",

    "src/transfer.sh": """#!/usr/bin/env bash

# Transfer functions
""",

    "src/validation.sh": """#!/usr/bin/env bash

# Validation functions
""",

    "src/logging.sh": """#!/usr/bin/env bash

# Logging functions
""",

    "tests/README.md": """# Tests

Répertoire destiné aux tests :

- unitaires
- intégration
- export
- import
- transfert
- validation
- erreurs
- dry-run
- reprise
- concurrence
""",

    ".gitignore": """logs/*
!logs/.gitkeep

work/*
!work/.gitkeep

*.dmp
*.dump
*.log

config/config.conf

__pycache__/
*.pyc
.venv/
venv/

.vscode/
.idea/

.DS_Store
Thumbs.db
""",

    "logs/.gitkeep": "",
    "work/.gitkeep": "",
}


def create_project(project_dir: Path, overwrite: bool = False):
    created = 0
    skipped = 0

    project_dir.mkdir(parents=True, exist_ok=True)

    for relative_file, content in FILES.items():

        file_path = project_dir / relative_file

        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists() and not overwrite:
            print(f"[SKIP]   {file_path}")
            skipped += 1
            continue

        file_path.write_text(content, encoding="utf-8")

        if file_path.suffix == ".sh":
            file_path.chmod(0o750)

        print(f"[CREATE] {file_path}")
        created += 1

    print()
    print("=" * 60)
    print("Projet Oracle Schema Duplicator créé")
    print("=" * 60)
    print(f"Répertoire : {project_dir}")
    print(f"Fichiers créés : {created}")
    print(f"Fichiers ignorés : {skipped}")
    print()
    print("Pour continuer :")
    print(f"cd {project_dir}")
    print("cat AGENTS.md")


def main():

    parser = argparse.ArgumentParser(
        description="Génère le projet Oracle Schema Duplicator"
    )

    parser.add_argument(
        "directory",
        nargs="?",
        default="oracle-schema-duplicator",
        help="Répertoire du projet",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Écraser les fichiers existants",
    )

    args = parser.parse_args()

    project_dir = Path(args.directory).expanduser().resolve()

    try:
        create_project(
            project_dir,
            overwrite=args.overwrite
        )

    except PermissionError as error:
        print(
            f"Erreur de permission : {error}",
            file=sys.stderr
        )
        sys.exit(1)

    except OSError as error:
        print(
            f"Erreur système : {error}",
            file=sys.stderr
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

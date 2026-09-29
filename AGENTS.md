# Oracle Schema Duplicator

## Mission

Développer un outil industriel permettant de dupliquer un schéma Oracle 19c
d'une base source vers une base cible.

## Environnement

- AIX
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

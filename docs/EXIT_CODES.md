# Codes de sortie

Table unique de vérité : `src/osd/exit_codes.py`. Partagée par la CLI, la
machine à états et le reporting. **Toute erreur se mappe sur exactement un
code.**

| Code | Constante | Libellé | Typiquement |
|------|-----------|---------|-------------|
| 0 | `SUCCESS` | succes | les 19 étapes ont abouti |
| 1 | `CONFIG` | configuration invalide | clé inconnue, valeur mal typée, fichier illisible |
| 2 | `PREREQ` | prerequis non satisfaits | binaire absent, tablespace inexistant, DIRECTORY introuvable, tablespace source manquant |
| 3 | `CONNECTION` | connexion impossible | `ORA-12154`, `ORA-12541`, wallet illisible, TNS_ADMIN non transmis |
| 4 | `EXPORT` | echec de l'export | `expdp` en échec, **et** dump tronqué ou incomplet à l'étape 12 |
| 5 | `TRANSFER` | echec du transfert | aucun backend disponible, copie impossible, partie absente après copie |
| 6 | `IMPORT` | echec de l'import | `impdp` en échec |
| 7 | `VALIDATION` | echec de la validation | objet invalide après import, volumétrie incohérente, réconciliation en écart |
| 8 | `SECURITY` | garde-fou de securite refuse | schéma cible déjà peuplé, `TABLE_EXISTS_ACTION` destructif sans `--allow-destructive`, `clean` sans autorisation |
| 9 | `INTERRUPTED` | interruption | SIGINT ou SIGTERM |

## Deux choix qui méritent d'être expliqués

### L'étape 12 rend 4, pas 6

La relecture du dump (`impdp SQLFILE=`) s'exécute sur la **source**, et
l'import n'a pas commencé. Un dump tronqué est un problème d'export : le remède
est de re-exporter, pas de rejouer un import qui n'a jamais eu lieu. Rendre ce
cas « échec de l'import » enverrait l'exploitant répétablement au mauvais
endroit.

C'est pourquoi `_assert_datapump` prend un `code` explicite plutôt que de le
déduire du nom de l'outil.

### « Objet existant » rend 8, pas 2

Un schéma cible déjà peuplé n'est pas un prérequis manquant : c'est une
opération qui n'a pas été explicitement demandée. `ALLOW_EXISTING_TARGET` est
là pour l'assumer. La nuance compte parce que les deux se traitent
différemment — un prérequis manquant se corrige en amont, une garde-fou
s'assume au moment du run.

## Codes de retour de Data Pump

Ce ne sont **pas** les codes de l'outil : ils sont interprétés puis projetés
sur la table ci-dessus. Rappel du seul fait qui surprend :

`DATAPUMP_SUCCESS_CODES = {0, 1, 2, 4, 8}`. Data Pump utilise plusieurs codes
pour un même succès (1 normal, 2 avec avertissement XML, 4 avec avertissement,
8 pour le client interactif). Les propager tels quels ferait échouer un export
réussi.

`5` est le seul code d'échec usuel ; il est projeté sur 4 ou 6 selon l'étape.

## Codes d'erreur Oracle

`ORA-`, `UDI-` et `DBMGSPC-` sont extraits de la sortie et **affichés tels
quels**. Leur détection ne dépend d'aucun texte traduit, donc elle fonctionne
quelle que soit la locale de l'hôte — ce qui est précisément le point : un
message Oracle traduit n'est pas analysable.

Les codes dont la cause est **prévisible** ont un remède spécifique, voir
`DATAPUMP_REMEDES`. C'est mieux que le remède générique — « consulter le
journal » — quand l'échec est entierement déterministe et que l'exploitant
n'a rien à investiguer.

| Code | Remède |
|------|--------|
| `ORA-31684` | un objet existe déjà ; `TABLE_EXISTS_ACTION` ne couvre que les tables |
| `ORA-39111` | l'import s'est arrêté ; conséquence du code précédent, pas une cause |

## Contrat de la CLI

`main()` ne lève jamais et ne rend **jamais** `None`. Une exception
inattendue devient un code, pas une trace Python. Le code rendu est toujours un
entier de la table ci-dessus.

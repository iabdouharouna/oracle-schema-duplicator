# Runbook

Ce document décrit ce que l'outil **fait réellement**, avec les codes de sortie
et les remèdes. Tout ce qui est ici a été observé sur une Oracle 19c réelle
(`expdp`/`impdp` en 19.0.0.0.0), pas déduit de la documentation.

## Lire un échec

Trois lectures, dans cet ordre.

**1. Le rapport** (`stdout`). Il porte le verdict, l'étape fautive, les détails
et le remède.

**2. La ligne « Erreur ».** Elle nomme l'étape, le code, les codes Oracle et le
remède. C'est cette section qui dit quoi faire.

**3. Le journal** (`LOG_DIR/run-<run-id>-*.log`). Une ligne par étape, et le
contenu des diagnostics.

Ce qui n'est **pas** dans le rapport : la sortie brute de `expdp`/`impdp`. Elle
est traduite dans la locale de l'hôte, donc inexploitable. Le rapport n'en
retient que les codes `ORA-`/`UDI-`, qui ne dépendent pas de la langue.

Pour la sortie brute, lire le journal Data Pump sur l'hôte, dans le répertoire
du `DIRECTORY` : `osd_<run-id>_export.log`, `osd_<run-id>_import.log`.

## Étape 2 — configuration

**Cle inconnue.** Le fichier est un parseur strict. Une faute de frappe est une
erreur, pas un avertissement : c'est la cause la plus longue à diagnostiquer
d'un run qui « marche mais ne fait rien ». Vérifier avec `osd config`, qui
liste le schéma complet.

**`SOURCE_SCHEMA` et `TARGET_SCHEMA` identiques.** Refusé sauf
`ALLOW_EXISTING_TARGET=true`. Un export puis import dans le même schéma n'est
pas une duplication.

## Étape 3 — dépendances

**`expdp` absent.** Le `PATH` du compte SSH n'est pas celui du compte
interactif. Un AIX pose typiquement le chemin Oracle dans `.profile`, que
`ssh <hote> sh -s` ne lit pas. Le remède nommé par l'outil est de vérifier le
`PATH` du compte exécuté, pas d'installer le client.

## Étape 4 et 5 — connexion

**`ORA-12154` / `ORA-12541`.** Trois causes, dans l'ordre de fréquence :

1. `SOURCE_TNS_ADMIN` / `TARGET_TNS_ADMIN` non renseigné alors que la
   connexion est un alias TNS. Le `TNS_ADMIN` du serveur de saut ne sert à rien
   — c'est celui de l'hôte qui compte, et l'outil le transmet.
2. wallet illisible, ou wallet hors de `osdwal` dans un `sqlnet.ora` modifié.
3. service_name inexistant ou instance arrêtée.

**Compte inexistant.** Un nom de schéma de compte n'est pas forcément un
utilisateur. L'étape 4 se connecte avec le compte de duplication ; c'est un
compte distinct du schéma dupliqué.

## Étape 6 et 7 — schémas

**Le schéma cible n'existe pas.** L'outil ne crée pas de compte : cela relève
de l'initialisation de la base. Le créer, puis relancer.

**Le schéma cible contient déjà des objets.** `ALLOW_EXISTING_TARGET` est
requis. C'est un garde-fou, pas un avertissement : un run quotidien qui écrase
silencieusement une cible préparée à la main est le pire des deux mondes.

**Si vous l'assumez, lisez quand même la suite** : `TABLE_EXISTS_ACTION=SKIP`
ne couvre que les tables. Voir « `ORA-31684` » plus bas.

## Étape 8 — tablespaces et privilèges

**Tablespace inexistant.** L'outil ne crée pas de tablespace. Vérifier
`REMAP_TABLESPACE` : une paire `SRC:DST` dont le `DST` n'existe pas côté cible
est un cas fréquent, et le message nomme les deux.

**Privilège absent.** L'étape interroge `SESSION_PRIVS` et dit lequel manque.
`EXP_FULL_DATABASE` n'est pas nécessaire si le compte n'exporte que ses propres
objets — l'outil l'exige seulement dans ce cas.

**`ORA-00942` sur `SESSION_PRIVS`.** La vue est accessible à tout compte
depuis longtemps. Son absence signale une base incomplète ou une session
étrangère (proxy) : vérifier manuellement sous le compte réellement utilisé.

## Étape 9 — espace

**Espace insuffisant.** L'estimation est faite sur les segments du schéma
source, avec une marge (`SPACE_MARGIN_PERCENT`, `SPACE_MARGIN_ABS_MB`). Elle
est une **borne basse** : les index, LOB et fragmentation ne sont pas mesurés
sur la source. Un refus n'est donc pas une garantie d'échec, mais un succès
n'est pas une garantie de place.

L'espace vérifié est celui du `DIRECTORY` côté source, puis celui des
tablespaces imposés côté cible. Les deux sont contrôlés parce qu'un export
échoue sur le premier bien avant l'import.

## Étape 11 — export

**Échec de `expdp`.** Consulter `osd_<run-id>_export.log` sur l'hôte. Causes
fréquentes : DIRECTORY non accessible en écriture, quota du tablespace dépassé,
`ORA-31641` (impossible de créer le fichier — place ou droits),
`ORA-39002` (objet déjà marqué en cours d'export par un autre job).

**Un fichier `-29.dmp` pour un `PARALLEL=4`.** Normal : Data Pump ne produit
qu'une partie tant que le dump tient en un seul fichier. Le `%d` de
`DUMPFILE` est un patron d'écriture, pas une promesse de N fichiers.

**Le listing est vide.** L'export a réussi mais l'énumération n'a rien trouvé.
Deux causes réelles : le préfixe de l'étape 11 ne correspond pas aux fichiers
réellement produits, ou le bloc machine n'a pas été écrit sur le descripteur 3.
Voir `docs/REMOTE_PROTOCOL.md`.

## Étape 12 — relecture du dump

Le dump est relu avec `SQLFILE=`, sur la **source**, sans rien écrire en base.
Un dump tronqué échoue explicitement :

| Code | Signification |
|------|---------------|
| `ORA-39002` | opération invalide |
| `ORA-39059` | jeu de fichiers incomplet |
| `ORA-39246` | table maîtresse introuvable |

Cette étape rend le code **4**, pas 6 : le remède est de re-exporter, pas de
rejouer un import qui n'a pas commencé.

**Les fichiers `.sql` et `.log` de cette étape sont supprimés au nettoyage**,
comme le dump. Ils sont dans le répertoire du `DIRECTORY` et personne d'autre
ne les nettoierait.

## Étape 13 — transfert

**« backend inconnu » alors que le dump est en place.** Cause historique : la
topologie était déduite du *mode configuré* (`TRANSFER_MODE`) au lieu de l'être
de la topologie réelle. Les deux côtés qui voient le même `DIRECTORY` n'ont
rien à transférer, quel que soit le mode. C'est désormais traité par
`TransferBackend.run()`.

**Une partie manquante après copie.** L'échec d'un `scp` doit rester
distinguable d'un succès : la taille de chaque partie est **revérifiée après**
copie. Une copie tronquée ne se voit pas au code de retour de `scp`.

**Aucun backend disponible.** `AUTO` sonde réellement (écriture, relecture,
suppression d'un fichier d'échantillon) au lieu de se fier à la présence du
binaire. Un `scp` installé peut ne pas fonctionner si le `sshd` n'expose pas
`sftp` — cas le plus courant sur AIX. Essayer `TRANSFER_MODE=SCP-LEGACY`.

## Étape 14 — import

### `ORA-31684` — un objet existe déjà

Le cas le plus fréquent, et **entièrement déterministe** : il n'y a rien à
investiguer.

Un export de schéma embarque l'objet `USER` du schéma exporté, que
`REMAP_SCHEMA` renomme. L'import cherchait donc à **créer le compte cible**,
qui existe par construction — l'étape 7 exige qu'il existe. L'outil pose donc
`EXCLUDE=USER` à l'import, et cette exclusion n'est pas surchargeable.

Sur un schéma cible **partiellement peuplé**, l'échec est différent et
inévitable : `TABLE_EXISTS_ACTION` ne s'applique **qu'aux tables**. Les
séquences, procédures, index, triggers et vues échouent tous, dans cet ordre.
`ORA-39111` accompagne l'arrêt ; il annonce la conséquence, pas la cause.

Deux issues, et il faut choisir :

- **duplication complète** — viser un schéma cible vide, ou le recréer :

  ```sql
  DROP USER <cible> CASCADE;
  CREATE USER <cible> IDENTIFIED BY ... DEFAULT TABLESPACE users;
  GRANT create session, create table, create view, create sequence, create procedure TO <cible>;
  ```

  puis relancer avec `ALLOW_EXISTING_TARGET=true`.

- **reprise partielle** — `TABLE_EXISTS_ACTION=SKIP` conserve les tables
  existantes. Attention : il ne touche ni aux autres objets, ni aux données
  déjà chargées. `APPEND` ajoute, `REPLACE` et `TRUNCATE` **écrasent** et
  exigent `--allow-destructive`.

### Tables maîtresses `SYS_EXPORT_TABLE_nn` survivant à l'import

**Limite connue, non contournable depuis l'extérieur.** `DBMS_DATAPUMP.REMOVE_JOB`
n'agit que sur le job de la **session courante** : le retirer depuis une autre
session exige de se reconnecter avec le même `userid` et le même nom de job.

Conséquence concrète : la table maîtresse verrouille le schéma, et un
`DROP USER` ultérieur exige **`CASCADE`**. Le job Data Pump, lui, est bien
supprimé par l'étape 18.

Inventer une variante ici sans pouvoir la tester serait pire que de le dire :
le symptôme d'une mauvaise réponse — un job que l'on croit supprimé et qui ne
l'est pas — ne se voit qu'au prochain export.

### `ORA-39208`

`TABLE_EXISTS_ACTION` est incompatible avec `SQLFILE`. L'outil ne pose jamais
les deux ensemble.

## Étape 15 et 16 — validation et comparaison

**Objet invalide après import.** Un objet invalide n'est pas un détail : c'est
un schéma qui ne compile pas côté cible. `STANDARD` échoue lui aussi, ce qui
est normal. L'import a réussi ; la **validation** est ce qui échoue, et le
remède est la sortie du journal d'import, pas un nouvel import.

**Écart de volumétrie.** Les lignes ne correspondent pas entre source et cible.
Cause habituelle : `TABLE_EXISTS_ACTION=SKIP` sur un schéma cible déjà
partiellement peuplé — les lignes déjà présentes n'ont pas été réimportées.

## Étape 18 — nettoyage

**« 0 artefact(s) supprimé(s) ».** Le nettoyage est best-effort : une
suppression qui échoue ne fait pas échouer le run. Un compte non nul confirme
que le DIRECTORY a été vidé ; un compte nul signifie qu'il reste quelque chose.

Sont supprimés : les parties du dump, le journal d'export, le journal d'import,
le `.sql` et le `.log` de la relecture (étape 12).

**Ce qui n'est pas supprimé :** les tables maîtresses Data Pump, ci-dessus.

**Après un échec, rien n'est supprimé.** C'est délibéré : le dump est la seule
chose qui permette de relancer l'import sans refaire l'export. Nettoyer sur un
échec obligerait à recommencer un run de plusieurs heures depuis le début.

**Après une reprise réussie, le nettoyage a lieu.** Le test est « cette
invocation a-t-elle échoué ? », et non « un échec a-t-il jamais eu lieu ? ».
Une reprise ne doit pas hériter du verdict de la tentative précédente.

## Interruption

`SIGINT` ou `SIGTERM` → code **9**, l'étape en cours est marquée en échec, le
dump est conservé. Un second signal termine immédiatement (`os._exit`) sans
laisser d'état à moitié écrit.

Le journal des étapes déjà validées est sur disque, donc la reprise ne
refait pas l'export.

## Diagnostic : le dump est-il complet ?

```sh
# cote source, sans ecrire en base
impdp userid=... SQLFILE=/dev/stdout DUMPFILE='<parties>' SCHEMAS=HR EXCLUDE=USER
```

Une relecture complète régénère tout le DDL. Sur un dump tronqué, elle échoue
avec `ORA-39059` ou `ORA-39246`.

## Diagnostic : l'état est-il cohérent ?

```sh
osd status -c config.conf --all
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d['steps'],indent=2))" \
  WORK_DIR/state-<run-id>.json
```

Le rapport JSON distingue `carried_over` : `true` signifie que l'étape vient
d'une tentative antérieure, pas de ce run.

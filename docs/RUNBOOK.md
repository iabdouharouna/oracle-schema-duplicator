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

**Un échec qui ne ressemble à aucun codes Oracle.** L'étape 4 ou 5 annonce
alors « aucune métadonnée retournée » alors que la base répond très bien. Ce
message désigne l'outil, et la cause est presque toujours dans le script
distant : il a échoué **avant** d'interroger Oracle — un argument manquant,
un client absent du `PATH`, un fichier temporaire impossible à créer.

Le script.remote écrit alors `OSD_FATAL=<cause>` dans le bloc de résultat, et
l'adaptateur Oracle le remonte tel quel. Si ce message réapparaît sous une
autre forme, regarder le `0403-` ou le `0404-` sur stderr dans le journal de
l'étape : ce sont les codes d'erreur du Bourne shell d'AIX, et ils nomment la
ligne fautive. Voir `docs/REMOTE_PROTOCOL.md` pour les particularités de ce
shell.

## Étape 2 — configuration

**Cle inconnue.** Le fichier est un parseur strict. Une faute de frappe est une
erreur, pas un avertissement : c'est la cause la plus longue à diagnostiquer
d'un run qui « marche mais ne fait rien ». Vérifier avec `osd config`, qui
liste le schéma complet.

**`SOURCE_SCHEMA` et `TARGET_SCHEMA` identiques.** Refusé sauf
`ALLOW_EXISTING_TARGET=true`. Un export puis import dans le même schéma n'est
pas une duplication.

## Étape 3 — dépendances

**`expdp` absent.** Deux causes, et l'ordre compte : vérifier d'abord le `PATH`
de l'hôte, ensuite le profil.

`Ni Ansible ni ssh n'ouvrent une session de connexion. Or un AIX pose
typiquement `ORACLE_HOME/bin` dans `/etc/profile` ou `~/.profile`, qu'une
coquille non interactive ne source pas. L'outil source ces fichiers au début de
chaque script — mais **seulement s'ils sont exécutables par le shell qui les
source**, ce que contrôle un `sh -n` préalable. Un `.profile` écrit en bash sur
un hôte dont `/bin/sh` est dash est donc ignoré, silencieusement.

```sh
# 1. le client est-il là, pour le compte du run ?
ansible osd_source -i inventory/hosts --vault-password-file /etc/osd/vault-pass \
       -m shell -a 'command -v expdp || echo ABSENT'

# 2. le profil est-il exploitable par /bin/sh ?
ansible osd_source -i inventory/hosts --vault-password-file /etc/osd/vault-pass \
       -m shell -a 'sh -n ~/.profile && echo PROPRE || echo ILLISIBLE'
```

`ILLISIBLE` au point 2 alors que le client existe au point 1 : le profil doit
être réécrit en POSIX sh. Ce n'est pas une Contrainte de l'outil, et c'est le
seul cas où la réponse est « modifier la configuration de l'hôte ».

## Étape 13 — transfert

**« aucun mécanisme de transfert disponible entre les deux hôtes ».** Les trois
backends ont été essayés et ont tous échoué ; le rapport en nomme un par un.
L'authentification est la cause dans la quasi-totalité des cas, et le transfert
passe **hors Ansible** — c'est ce qui le distingue de toutes les autres étapes.

| Message | Cause |
|---------|-------|
| `Permission denied (publickey,...)` | clé absente du `authorized_keys` de l'hôte, ou `ansible_user` ne correspond à aucun compte |
| `Could not resolve hostname osd_source` | `ansible_host` absent de l'inventaire : le nom d'inventaire n'est résolvable que par Ansible |
| `no identity file` / `Host key verification failed` | `ansible_ssh_private_key_file` non posé, ou hors `known_hosts` |
| `Host key verification failed` sur une clé *connue* | clé d'hôte **changée** : c'est le cas qui doit alerter, pas celui qu'on contourne avec `StrictHostKeyChecking=no` |

Le nom d'hôte employé par le transfert vient de `ansible_host`, et non du nom
d'inventaire. Un inventaire qui sépare le nom de l'adresse — la pratique
recommandée, puisque le nom survit à un changement d'IP — est le cas normal ;
c'est le transfert qui doit traduire.

**`rsync` échoue avec `Could not resolve hostname connecttimeout=10`.** Ce
message ne parle que de résolution de nom, alors que la cause est une ligne de
commande mal construite : `rsync` découpe la valeur de son `-e` sur les espaces
et la passe à un shell, donc `-o` doit précéder **chaque** option. Ce défaut a
été masqué en production par le repli automatique vers `scp`, qui lui succeed :
le run passait, avec un backend différent de celui demandé.

**Le transfert n'a pas de dialogue possible.** En mode mot de passe, `BatchMode`
est retiré de l'inventaire et remplacé par `NumberOfPasswordPrompts=1`. Un
inventaire qui pose `BatchMode=yes` *et* `ansible_password` ne peut donc pas
s'authentifier : les deux options se contredisent, et l'erreur est une
authentification refusée, sans mention de leur contradiction.

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

**Le schéma cible n'existe pas.** Refusé par défaut : créer le compte, puis
relancer — ou poser `CREATE_TARGET_SCHEMA=true` pour que l'outil le crée à
l'image du schéma source : tablespace par défaut et temporaire (après
`REMAP_TABLESPACE`), profil, privilèges système, rôles, quota, et empreinte du
mot de passe (`IDENTIFIED BY VALUES`, jamais en clair).

L'outil ne crée **pas** de tablespace : un tablespace par défaut ou temporaire
absent côté cible est un échec prerequis (code 2), avec remède. Un profil
absent retombe sur `DEFAULT`, un rôle absent est ignoré avec un avertissement.

`check` et `--dry-run` ne créent rien : ils annoncent « sera créé », puis
arrêtent au périmètre normal (étapes 1 à 9 pour `check`).

**Le schéma cible contient déjà des objets.** `ALLOW_EXISTING_TARGET` est
requis. C'est un garde-fou, pas un avertissement : un run quotidien qui écrase
silencieusement une cible préparée à la main est le pire des deux mondes.

**Si vous l'assumez, lisez quand même la suite** : `TABLE_EXISTS_ACTION=SKIP`
ne couvre que les tables. Voir « `ORA-31684` » plus bas.

## Filtrage des tables

Quatre clés de liste, deux modes — et ils ne disent pas la même chose. Une
cinquième clé, un booléen, alimente le second mode.

| Clés | Effet |
|------|-------|
| `EXCLUDE_TABLES`, `EXCLUDE_TABLES_REGEX` | la table ne part **pas du tout** : ni structure ni données |
| `EXCLUDE_TABLES_DATA`, `EXCLUDE_TABLES_DATA_REGEX` | la structure part, les lignes non |
| `EXCLUDE_STANDALONE_TABLES` | la structure part, les lignes non — pour toutes les tables **isolées et inutilisées** du schéma |

Les listes portent des noms séparés par des virgules ; les deux clés `_REGEX`
sont des motifs `re` Python, ignorés à la casse (`^TMP_` désigne toutes les
tables commençant par `TMP_`).

**Ce qui part avec une table du mode A.** Data Pump l'omet avec ses index,
contraintes, grants et triggers. L'outil retire aussi les objets qui en
dépendent — vues, procédures, packages — directement puis transitivement :
créer une vue sur une table absente échouerait en `ORA-00942` à l'import, et
une procédure ne se compile pas sans les tables qu'elle référence. Ce qui est
retiré est listé à l'étape 6, jamais silencieux.

**Entrée sans correspondance : avertissement, pas échec.** Une table peut
changer de nom entre deux runs ; refuser le run sur ce signal empêcherait
précisément l'exécution qui l'aurait fait apparaître. Le message nomme
l'entrée et dit quoi corriger. Une regex **mal formée**, elle, est refusée au
chargement de la configuration (code 1) — elle ne sera jamais juste.

**Une table dans les deux listes.** L'exclusion totale l'emporte, et
l'étape 6 le signale.

**Mode B et intégrité référentielle.** Exclure les données d'une table
**parente** alors qu'une de ses tables **enfants** garde les siennes produit
une cible où les clés parentes manquent. `impdp` charge tout ce qu'il peut,
puis ne peut pas *valider* les clés étrangères concernées :
`ORA-02298: cannot validate (...FK...) - parent keys not found`, enveloppé
par `ORA-39083`. C'est la conséquence **attendue** du mode B, pas un défaut
du dump. L'outil ne fait donc pas échouer le run lorsque le mode B est actif
et que **tous** les codes d'erreur de l'import appartiennent à
`{ORA-02298, ORA-39083}` ; il rapporte les contraintes restées non créées
dans un avertissement `contraintes referentielles` et poursuit. Un code
étranger, ou un job d'import qui n'est pas `COMPLETED`, ramène l'échec
ordinaire. Pour éviter le cas : exclure aussi les données des tables enfants,
ou ne pas exclure celles des parents. Mesuré sur une 19c réelle (`HR` :
`JOB_HISTORY`, 11 lignes, vers `EMPLOYEES` et `DEPARTMENTS`, 0 ligne).

**Tables isolées — `EXCLUDE_STANDALONE_TABLES=true`.** Une clé booléenne qui
n'est pas un troisième mode : elle alimente le mode « structure sans données »
et y verse les tables **isolées et inutilisées** du schéma source. Deux
conditions doivent tenir ensemble :

1. **aucune FK ne la relie**, ni en sortie (elle ne référence rien) ni en
   entrée (aucune contrainte, d'aucun schéma, ne la référence) — exactement
   `NOT EXISTS` des deux côtés de `DBA_CONSTRAINTS` ;
2. **aucun objet autre qu'une table ne la lit** : vue, procédure, fonction,
   package, type, synonyme, ou trigger d'une **autre** table, lu dans
   `DBA_DEPENDENCIES`. Le trigger posé sur la table elle-même **ne compte
   pas** : il fait partie de son DDL et voyage avec elle.

Le second volet est essentiel : l'absence de FK ne dit pas qu'une table est
morte. Une table sans relation peut très bien être lue par un package, et
retirer ses lignes viderait une donnée que l'application attend. La règle ne
vise que les tables que **rien** n'utilise.

Cette règle **ne peut pas** provoquer `ORA-02298` : une table isolée n'ayant
aucune relation référentielle, retirer ses lignes ne laisse aucune clé parente
manquante, donc aucune FK à ne pas valider. La conséquence du mode B décrite
plus haut ne s'applique pas ici.

**À activer en connaissance de cause.** La règle est volontairement
conservatrice, et c'est le second volet qui la resserre le plus : sur un schéma
applicatif, beaucoup de tables sans FK sont en réalité lues par du code. Mesuré
sur un schéma réel de 7 023 tables : 5 634 sont isolées par FK, mais **1 726
seulement** le sont aussi de tout lecteur — dont les triggers propres comptent
pour l'exclusion inverse (un trigger posé sur la table ne la disqualifie pas).
Autrement dit, l'espace non copié varie fortement selon qu'on compte ou non les
lecteurs — et c'est ce que l'estimation de l'étape 9 reflète. `osd check` en
donne la liste exacte sans rien exporter : c'est la vérification à faire avant
de l'activer. Comme tout filtre, elle est résolue à l'étape 6 puis figée ; un
schéma entièrement relié ou intégralement lu par du code donne une liste vide,
que l'étape 6 signale sans échec.

**Limite du second volet.** `DBA_DEPENDENCIES` ne connaît que les références
**statiques**. Un package qui nomme une table par SQL dynamique
(`EXECUTE IMMEDIATE`) n'y figure pas, et l'outil ne lit pas le code source pour
le deviner : la table serait alors classée isolée à tort, ses lignes non
copiées. Un schéma dont l'application lit des tables de données en SQL
dynamique doit laisser `EXCLUDE_STANDALONE_TABLES=false`.

**`INCLUDE` est incompatible avec `EXCLUDE` et `EXCLUDE_TABLES*`** : la 19c
refuse les deux dans le même parfile (`UDE-00011: parameter include is
incompatible with parameter exclude`, mesuré sur une base réelle). La
combinaison est refusée au chargement. `EXCLUDE_TABLES_DATA*` — un `query=` —
n'est pas concerné et se cumule sans problème à `INCLUDE`.

**Le plan est résolu à l'étape 6, contre le schéma source réel, puis figé
dans l'état du run.** Un `resume` rejoue donc la même liste que l'export
qu'il rejoue, même si le schéma a changé entre temps.

**Le filtre ne supprime rien de ce qui existe déjà côté cible** : il décide
de ce qui part, pas de ce qui reste.

**`CONTENT=METADATA_ONLY`** : `EXCLUDE_TABLES_DATA*` n'a alors aucun effet —
aucune ligne n'est exportée de toute façon. La combinaison est acceptée, avec
un avertissement au chargement.

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

**« Espace non mesurable » alors que le chemin existe.** C'est le symptôme
d'un `df -k` mal lu, pas d'un disque plein. Sur AIX, `df -k` n'affiche **pas**
la colonne `Used` : `Free` occupe le troisième champ et le quatrième est le
pourcentage d'occupation. Un lecteur qui prend les champs à position fixe
comme sur Linux obtient « 42% » là où il attendait un compte de blocs, et la
validation rejette le résultat.

Le corps de `remote_space.sh` reconnaît donc la disposition sur le caractère du
quatrième champ, et publie `OSD_DF_LAYOUT` (`aix` ou `posix`) pour que la
cause reste visible. Si ce contrôle réapparaît après une mise à jour, c'est
que la détection a été perdue : vérifier les colonnes réelles du `df` de
l'hôte avant de conclure à un problème de place.

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

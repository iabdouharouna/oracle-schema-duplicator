# Exploitation

## Où s'exécute quoi

| Où | Quoi |
|----|------|
| Serveur de saut (Linux) | Python 3.9+, `ansible`, `ssh`, `scp`/`rsync`/`sftp`. **Pas de client Oracle.** |
| Hôte AIX de la base | Oracle Client 19c, `sqlplus`, `expdp`, `impdp`, `sshd`. Pas de Python. |

`bin/osd` se lance sur le serveur de saut. Il ne lit jamais la base : il décide
et compare, et confie toute lecture au client Oracle de l'hôte concerné.

## Prérequis côté AIX

```sh
which sqlplus expdp impdp sh awk     # exigés
# sshd doit accepter l'authentification retenue par l'inventaire
```

Ce `which` doit être exécuté **dans les conditions du run**. Ni Ansible ni
`ssh` n'ouvrent de session de connexion, et le `PATH` du compte
d'exploitation — où l'installeur Oracle dépose `ORACLE_HOME/bin` — n'est donc
pas chargé. L'outil le source lui-même, au début de chaque script ; sans lui,
le contrôle annoncerait un client absent sur un hôte où il est installé. Voir
`docs/REMOTE_PROTOCOL.md`.

Le compte SSH du serveur de saut doit avoir :

- l'accès en lecture/écriture au répertoire du `DIRECTORY` Data Pump, via
  l'écriture par `expdp` (le dump n'est pas lisible par `scp` autrement) ;
- l'authentification retenue par l'inventaire : clé privée, ou mot de passe
  chiffré par Vault.

## Inventaire et coffre

C'est l'inventaire qui décide **qui** est exécuté, et par quel compte. Il
remplace les clés `SSH_KEY` et `*_SSH_USER` de la configuration, qui sont
refusées si elles portent une valeur.

| Clé de configuration | Rôle |
|----------------------|------|
| `OSD_INVENTORY` | fichier d'inventaire Ansible (défaut `inventory/hosts`) |
| `OSD_VAULT_PASSWORD_FILE` | mot de passe qui déchiffre le coffre, hors dépôt, en `0600` |

Mise en place, une seule fois par serveur de saut :

```sh
# 1. le gabarit, versionné et lisible sans déchiffrer
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
$EDITOR inventory/group_vars/all.yml          # ansible_password ou la clé

# 2. le mot de passe du coffre, hors du dépôt
install -m 600 /dev/null /etc/osd/vault-pass
$EDITOR /etc/osd/vault-pass

# 3. le chiffrement
ansible-vault encrypt inventory/group_vars/all.yml
```

Ordre impératif : le mot de passe de coffre ne doit pas être créé *après* le
chiffrement, sinon le premier `run` échoue sur un déchiffrement, et le message
— « ciphertext password verification failed » — ne dit pas que le fichier
n'existe pas encore.

Vérification avant le premier run réel :

```sh
ansible osd_source -i inventory/hosts --vault-password-file /etc/osd/vault-pass \
       -m debug -a 'msg={{ ansible_user | default("") }}'
```

Cette commande teste exactement ce que l'outil fera : elle doit renvoyer le
compte, pas une erreur.

### Rotation du secret SSH

```sh
ansible-vault edit inventory/group_vars/all.yml   # changer ansible_password
ansible all -i inventory/hosts --vault-password-file /etc/osd/vault-pass \
           -m ping
```

Si le compte est verrouillé ou la clé révoquée,
`ansible ... -m ping` échoue en quelques secondes. Le faire **avant** de changer
le mot de passe sur les deux hôtes évite l'inverse : un hôte mis à jour, l'autre
non, et un run qui réussit sur l'un et échoue sur l'autre — sans que rien dans
le rapport ne dise lequel des deux est en retard.

### Rotation du mot de passe de coffre

```sh
ansible-vault rekey --new-vault-password-file /tmp/nouveau \
                    --vault-password-file /etc/osd/vault-pass \
                    inventory/group_vars/all.yml
install -m 600 /tmp/nouveau /etc/osd/vault-pass && shred -u /tmp/nouveau
```

`rekey` chiffre à nouveau avec la **même** clé de contenu : seul change le
vocabulaire de chiffrement. Sans cette étape, changer le mot de passe seul rend
le fichier indéchiffrable, et le run échoue sur le premier hôte.

## Connexion Oracle

Trois formes sont acceptées, par `SOURCE_CONNECT` et `TARGET_CONNECT` :

| Forme | Exemple | TNS_ADMIN |
|-------|---------|-----------|
| EZCONNECT | `host:1521/SERVICE` | inutile |
| alias TNS | `L_PROD` | `SOURCE_TNS_ADMIN` / `TARGET_TNS_ADMIN` |
| SCAN | `(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=scan...)(PORT=1521))...)` | inutile |

Pour un alias TNS, `SOURCE_TNS_ADMIN` est **transmis à l'hôte** : c'est le
`TNS_ADMIN` local du serveur de saut qui ne sert à rien, et son oubli produit
un `ORA-12154` que rien dans le rapport ne permet de rattacher à sa cause.

### Authentification OS

`SOURCE_OS_AUTH=true` (resp. `TARGET_OS_AUTH=true`) remplace **les deux**
formes précédentes par une connexion locale `/` : l'identité est celle du
compte d'exploitation (`oracle`, souvent) sur la machine qui exécute le
client. Elle ne convient que si ce compte a les droits nécessaires — mesuré :
`/ as sysdba` répond, `/@alias as sysdba` est refusé en `ORA-01017`.

La combinaison avec `_CONNECT` ou `_WALLET` est **refusée à la lecture** : une
chaîne ou un wallet désignent une identité qui contredit `/`. Le rapport
affiche alors `authentification OS` là où une chaîne serait montrée.

## Comptes et secrets

Le compte connecté est un compte **de duplication**, pas `SYS`. Il lui faut :

- `EXP_FULL_DATABASE` côté source ;
- `IMP_FULL_DATABASE` côté cible ;
- ou, à défaut, au moins `READ,WRITE` sur le `DIRECTORY` utilisé.

L'étape 8 interroge `SESSION_PRIVS` et dit lequel manque. Elle n'échoue pas sur
un droit absent : un compte sans `EXP_FULL_DATABASE` peut tout de même exporter
son propre schéma, et le refus serait alors unjustifié.

**Le mot de passe ne va jamais dans un mot de passe en clair.** Trois voies,
par ordre de préférence :

1. **Wallet externe** — `SOURCE_WALLET` / `TARGET_WALLET`, chemin du wallet.
   Aucune chaîne de connexion n'est alors composée par l'outil.
2. **Wallet interne** — le `DIRECTORY` wallet d'Oracle (`cwallet.sso`,
   `tnsnames.ora`).
3. `SOURCE_PASSWORD` / `TARGET_PASSWORD` — **repli seulement**, et la valeur
   n'est jamais écrite dans un journal, un rapport, un état ou un argument de
   processus. Elle est masquée par `redact.py` partout où un texte est produit.

Le `SOURCE_USER` peut être une chaîne de connexion complète
(`"HR"/****@L_PROD`) ou le seul nom du compte lorsque le mot de passe est
porté par un wallet.

## Configuration

Le schéma complet s'affiche par :

```sh
osd config            # toutes les clés, avec défauts et avertissements
osd config EXCLUDE    # une clé, en détail
```

Copier `config/config.example.conf` vers `config/config.conf` et l'adapter.
Ce fichier est un **parseur strict**, pas un script shell :

```ini
CLE=VALEUR
CLE="VALEUR avec espaces"
```

Refusés : `export X=`, `$(...)`, backquotes, `;`, `&&`, `||`, `<`, `>`, heredoc,
continuation par `\`. Une clé inconnue est une **erreur** (code 1), pas un
avertissement : une faute de frappe silencieuse est la cause la plus longue à
diagnostiquer d'un run qui « marche mais ne fait rien ».

Priorité, de la plus faible à la plus forte :

```
défaut du schéma  <  fichier  <  OSD_<CLE>  <  --set CLE=VALEUR
```

## `SOURCE_SCHEMA` et `TARGET_SCHEMA`

`REMAP_SCHEMA` est **toujours** appliqué (`SOURCE_SCHEMA` → `TARGET_SCHEMA`).
Aucune option ne permet de le désactiver : une duplication vers le même nom
n'a de sens qu'en acceptant explicitement d'écraser la cible.

Les deux noms ne peuvent pas coïncider, sauf `ALLOW_EXISTING_TARGET=true`. Le
cas est refusé à la lecture : un export puis import dans le même schéma n'est
pas une duplication, et l'oubli de `TARGET_SCHEMA` n'apparaîtrait qu'après
l'avoir constaté.

**L'outil ne crée pas de compte.** La création du schéma cible relève de
l'initialisation de la base. L'étape 7 exige qu'il existe, et le dit ainsi
plutôt que d'échouer à l'import sur un `ORA-01435`.

## Exécution

```sh
osd check  -c config.conf          # étapes 1 à 9, rien n'est écrit
osd run    -c config.conf          # le run complet
osd resume -c config.conf --run-id <id>   # après une interruption
osd status -c config.conf --all    # tous les runs
osd clean  -c config.conf --allow-destructive
```

Options communes : `--set CLE=VALEUR`, `--json`, `--verbose`, `--quiet`.
`--dry-run` n'existe que sur `run` : `check` n'a rien à simuler, lui donner
l'option ferait croire le contraire.

`stdout` ne porte **que** le rapport. Les journaux vont sur `stderr`. Un
ordonnanceur capture donc le rapport sans y trouver de trace de diagnostic.

## `--dry-run`

Ce n'est pas une simulation approximative : le runner est remplacé par un
`NullRunner` qui enregistre les appels sans les exécuter, et le chemin de code
parcouru est **exactement** le chemin réel.

Ce qui est réellement exécuté : connexion aux deux bases, inventaire des
schémas, privilèges, tablespaces, espace disponible.

Ce qui ne l'est pas : `expdp`, la relecture du dump, le transfert, `impdp`, et
le nettoyage. Le rapport les liste sous « mutations retenues ».

Le nettoyage est le cas le plus important : il est déclaré `mutating=True`, donc
en simulation il laisse le dump **intact**. Le dump est la seule chose qui
rende le run reprenable après un échec ; le supprimer en dry-run serait
exactement l'erreur que la simulation doit éviter.

## Cron

```cron
17 2 * * *  cd /srv/osd && ./bin/osd run -c config/config.conf >> /var/log/osd.log 2>&1
```

Points vérifiés pour cet usage :

- `--set` et `-c` sont lus, jamais exécutés ;
- `BatchMode=yes` est garanti par la validation en mode clé, donc aucun
  dialogue possible. En mode mot de passe, il est **retiré** de l'inventaire et
  remplacé par `NumberOfPasswordPrompts=1` : `BatchMode` et le mot de passe
  sont mutuellement exclusifs, et garder les deux ferait échouer toute
  authentification par mot de passe ;
- le secret transite par l'environnement (`SSHPASS`), jamais par `-p`, donc
  jamais dans la table des processus ;
- le verrou (`LOCK_DIR`) empêche deux runs concurrents ; `LOCK_DIR` doit être
  **local**, pas NFS — `fcntl` sur NFS n'est pas fiable ;
- le rapport et le journal sont horodatés : plusieurs runs ne s'écrasent pas ;
- le code de sortie est 0–9, exploitable tel quel par l'ordonnanceur.

`TRANSFER_MODE=AUTO` sonde les backends à chaque run (sonde d'une heure) : un
`sshd` reconfiguré entre deux runs ne peut pas provoquer un échec inexpliqué.

L'environnement du run est réduit à ce qu'Ansible exige :

```
ANSIBLE_FORCE_COLOR=0   ANSIBLE_NOCOLOR=1   ANSIBLE_RETRY_FILES_ENABLED=0
```

`ANSIBLE_NOCOLOR` n'est pas là pour la lisibilité seulement. Une séquence
d'échappement insérée dans la sortie de `debug` -- donc dans le mot de passe lu
pour le transfert -- le corromprait silencieusement. `HOME` est conservé, parce
que c'est là qu'Ansible cherche `~/.ansible/cp`, sans quoi chaque appel recrée
un contexte.

`LC_ALL` n'est pas forcé : Ansible 2.14 refuse un `LC_ALL=C` transmis par
l'environnement. Le forcer produisait un avertissement sur `stderr` à chaque
appel, sans rien apporter.

## Reprise

```sh
osd status -c config.conf --all                 # repérer le run
osd resume -c config.conf --run-id <id>         # reprendre
osd resume -c config.conf --run-id <id> --force # tout rejouer
```

`resume` ne rejoue que ce qui n'a pas été validé. Le dump étant conservé après
un échec — c'est ce qui rend la reprise possible — un `resume` n'a pas à
refaire l'export.

Le rapport distingue `[OK   ]` — exécuté par ce run — de `[REPRISE]` — relu
dans l'état d'une tentative antérieure. Cette distinction est aussi dans le
JSON (`carried_over`), parce qu'un ordonnanceur lit le JSON.

Après une reprise **réussie**, le nettoyage a lieu comme après n'importe quel
succès : le dump n'est plus la seule voie de recovery.

## Codes de sortie

Voir `docs/EXIT_CODES.md`. Rappel : `main()` ne lève jamais et ne rend jamais
`None` — une exception inattendue devient un code, pas une trace Python.

## Fichiers produits

| Quoi | Où |
|------|-----|
| état | `WORK_DIR/state-<run-id>.json` |
| journal | `LOG_DIR/run-<run-id>-<horodatage>.log` |
| rapport | `REPORT_DIR/report-<run-id>-<horodatage>.txt` (ou `.json`) |
| dump, journaux Data Pump | répertoire du `DIRECTORY`, **chez la base** |

`WORK_DIR`, `LOG_DIR` et `REPORT_DIR` sont en `0700` / `0600` : ils n'ont aucun
secret à afficher, mais un état contient les noms d'objets du schéma du client.

`osd clean` supprime état, journaux et rapports. C'est destructif — il faut
`--allow-destructive` — et sans lui l'outil **liste** ce qu'il supprimerait.

# Exploitation

## Où s'exécute quoi

| Où Quoi |
|---------|--------|
| Serveur de saut (Linux) | Python 3.9+, `ssh`, `scp`/`rsync`/`sftp`. **Pas de client Oracle.** |
| Hôte AIX de la base | Oracle Client 19c, `sqlplus`, `expdp`, `impdp`, `sshd`. Pas de Python. |

`bin/osd` se lance sur le serveur de saut. Il ne lit jamais la base : il décide
et compare, et confie toute lecture au client Oracle de l'hôte concerné.

## Prérequis côté AIX

```sh
which sqlplus expdp impdp sh awk     # exigés
# sshd doit accepter BatchMode + clé (voir plus bas)
```

Le compte SSH du serveur de saut doit avoir :

- une clé publique dans `~/.ssh/authorized_keys` ;
- l'accès en lecture/écriture au répertoire du `DIRECTORY` Data Pump, via
  l'écriture par `expdp` (le dump n'est pas lisible par `scp` autrement) ;
- `BatchMode` accepté — sans quoi une erreur d'authentification ouvre une
  invite et le run reste bloqué jusqu'à l'expiration du crontab.

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
- `BatchMode=yes` est garanti par la validation, donc aucun dialogue possible ;
- le verrou (`LOCK_DIR`) empêche deux runs concurrents ; `LOCK_DIR` doit être
  **local**, pas NFS — `fcntl` sur NFS n'est pas fiable ;
- le rapport et le journal sont horodatés : plusieurs runs ne s'écrasent pas ;
- le code de sortie est 0–9, exploitable tel quel par l'ordonnanceur.

`TRANSFER_MODE=AUTO` sonde les backends à chaque run (sonde d'une heure) : un
`sshd` reconfiguré entre deux runs ne peut pas provoquer un échec inexpliqué.

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

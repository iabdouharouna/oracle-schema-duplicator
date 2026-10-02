# Protocole d'exécution distante

Ce document décrit le contrat entre le serveur de saut et l'hôte qui porte la
base. Il est implémenté par `shell/prelude.sh` et `src/osd/runner.py`, et
vérifié par `tests/unit/test_protocol.py`.

## Transport

L'exécution distante passe par **Ansible**, en simple transport. Le script
n'est pas un script Ansible : c'est le même script POSIX qu'avant, écrit dans
un fichier `0600` éphémère, et Ansible ne fait que l'exécuter sur l'hôte
désigné par l'inventaire.

```
ansible <hote> -i <inventaire> --vault-password-file <fichier>
       -m script -a <script> -- <args>
```

Ce choix mérite d'être justifié, car l'autre — réécrire les dix-neuf étapes en
tâches Ansible — était défendable.

**Ce qu'Ansible apporte** est exactement ce qui manquait :
l'authentification par coffre chiffré, la lecture d'un inventaire, et un seul
endroit où se déclarent les hôtes, les comptes et les clés. Les identifiants
SSH ne sont donc plus dans la configuration de l'outil, donc plus dans une
sauvegarde ordinaire.

**Ce qu'Ansible n'apporte pas** est le protocole : le bloc de résultat
`OSD_RESULT_BEGIN` / `OSD_RESULT_END`, le code de retour, la remontée des
codes ORA. Ces éléments sont validés sur instance réelle depuis le début du
projet, et les réécrire en tâches n'aurait rien apporté d'autre que le risque
de les régresser.

Le pipeline reste donc responsable de l'ordre des étapes : Ansible est
l'infrastructure, pas l'automate.

### Le code de retour ne vient pas d'Ansible

`ansible -m script` renvoie `rc=0` **même quand le script sort en erreur**.
Vérifié empiriquement, et c'est le piège qui ferait passer un export raté pour
un succès.

Le code réel est donc lu dans le bloc de résultat :

```
osd_finish 3   ->  OSD_RESULT_END rc=3
```

Le `rc` d'Ansible est ramené à `0` puis ignoré. Le refus d'un bloc sans
`rc=` est explicite : un bloc tronqué — script tué, connexion perdue — ne se
distingue pas d'un succès.

### La sortie est récupérée par le descripteur 3

Le protocole redirige la sortie de l'outil vers `fd 3` (`exec 3>&1`) et
laisse `stdout` au client, sinon Ansible l'encombrerait de son propre
`SUCCESS => {...}`. Le corps des scripts écrit par `osd_kv`, qui vise `fd 3` ;
c'est ce descripteur qu'Ansible récupère intact.

### Ce que l'inventaire fournit, et à qui

L'inventaire (`OSD_INVENTORY`) alimente deux chemins de nature différente, et
c'est la source de plusieurs défauts dont les symptômes ne désignaient pas la
cause :

| Besoin | Consommateur | Variable |
|--------|--------------|----------|
| Exécuter | Ansible | `ansible_host`, `ansible_user` |
| Options SSH du transfert | `scp`/`rsync`/`sftp` | `ansible_ssh_common_args` |
| Adresse du transfert | `scp`/`rsync`/`sftp` | `ansible_host` |
| Compte du transfert | `scp`/`rsync`/`sftp` | `ansible_user` |
| Clé privée | `scp`/`rsync`/`sftp` | `ansible_ssh_private_key_file` |

Le transfert est **hors Ansible** : `scp`, `rsync` et `sftp` sont des clients
du serveur de saut et ignorent l'inventaire. Il en découle trois traductions
obligatoires :

- le **nom d'inventaire** n'est résolvable que par Ansible. Sans
  `ansible_host` traduit en adresse, la commande porterait
  `scp osd_source:/...` et échouerait sur une résolution de nom — après douze
  étapes réussies et un export achevé ;
- `ansible_ssh_private_key_file` est une variable **seule**, que
  `ansible_ssh_common_args` ne porte pas. Traduite en `IdentityFile`, sinon un
  inventaire authentifié par clé — le mode recommandé — exécuterait tout le
  run puis échouerait à la copie ;
- `ansible_ssh_args` n'est **pas** repris : un `-i` posé là est lu par
  Ansible et ignoré par le transfert. Le gabarit le dit.

Le partage du secret se fait par `ansible -m debug`, avec le joker posé dans
l'expression :

```
msg={{ ansible_password | default("") }}
```

Sans le joker, une variable absente ne produit aucun statut d'échec
exploitable : `ansible -m debug` renvoie `msg` avec le texte « the task
includes an option with an undefined variable », `failed` restant vide. Ce
texte deviendrait alors le mot de passe remis à `sshpass`, et l'échec
n'apparaîtrait qu'à la copie, sous la forme d'une authentification refusée
sans lien visible avec sa cause.

## Profil de connexion

Chaque script commence par sourcer le profil de l'hôte :

```sh
for _osd_prof in /etc/profile "$HOME/.profile" "$HOME/.profile.ksh"; do
    [ -r "$_osd_prof" ] || continue
    sh -n "$_osd_prof" >/dev/null 2>&1 || continue
    . "$_osd_prof" >/dev/null 2>&1 || :
done
export PATH
```

Ce n'est pas une précaution de confort. Un client Oracle sur AIX est dans le
`PATH` du compte d'exploitation, posé par ces fichiers, et **ce que nous
lançons n'est ni une session interactive ni une session de connexion** :
Ansible comme `ssh` ouvrent une coquille non interactive, qui ne source rien.
Sans ce bloc, `expdp` paraît absent sur un hôte où il est installé, et le
remède nommé par l'étape 3 — vérifier le `PATH` du compte — n'a rien à
vérifier puisque c'est précisément le `PATH` de l'exploitant qui fait
défaut.

Quatre points, chacun parce que son défaut a été observé :

- **avant le prelude, et protégé** — un profil ne déclare pas toujours les
  variables qu'il initialise, et peut employer une construction propre à sa
  coquille de connexion. Sourcé après `set -u`, il tuerait le script avant le
  prelude, donc avant tout résultat : un échec muet. Le `|| :` et le
  `sh -n` le rendent inerte.
- **validé par `sh -n`** — une erreur de syntaxe n'est pas un échec
  d'exécution, elle tue le shell courant et `|| :` n'y change rien. Le cas
  réel est un `.profile` écrit en bash sur un hôte dont `/bin/sh` est dash :
  il casse **tous** les scripts. Le contrôle ne laisse passer que ce que le
  shell qui va le sourcer sait exécuter.
- **sourcé dans ce shell**, pas dans un sous-shell — un `$( . "$p" )`
  isolerait le `PATH` mais perdrait `ORACLE_HOME`, sans lequel `sqlplus`
  échoue en SP2-0750. Le prix de l'erreur de syntaxe est donc un profil
  **ignoré**, pas un script perdu.
- **`PATH` ré-exporté** — un profil l'affecte sans toujours l'exporter, et
  une variable non exportée n'agit sur aucune commande qui suit la source.

Aucun de ces fichiers n'est obligatoire, et le coût est de trois appels à
`sh -n` par script.

## Assemblage

`build_script(body, argv, bootstrap=..., env=...)` concatène dans cet ordre :

```
environnement → prelude → amorçage → arguments → corps
```

L'ordre est significatif, et les deux inversions possibles sont des erreurs
réellement observées :

- **`env` avant le prelude** — le client Oracle est lancé par le corps ; une
  valeur posée après le prelude n'aurait d'effet sur rien.
- **`bootstrap` avant les arguments** — un `Raw` ne peut désigner qu'une
  variable **déjà posée**. C'est ainsi que le parfile est écrit par
  l'amorçage puis désigné par `Raw('"$osd_parpath"')`. Inversé, le script
  évalue `osd_parpath` sur une variable jamais posée et meurt avant l'export.

`bootstrap` ne peut donc pas référencer `osd_argN`, et aucun appelant n'en a
besoin.

Les variables `osd_argN` sont recombinées par `set --` pour que le corps
utilise `"$@"` comme un script normal. `Raw` est réservé aux valeurs déjà
définies ; tout le reste est mis entre guillemets simples.

## Contraintes du shell

Sur AIX 7.2, `/bin/sh` est le **Bourne shell** — mesuré sur les deux hôtes
de l bank's d'essai : `/bin/sh` et `/usr/bin/sh` y sont le même binaire, et
`KSH_VERSION` n'y est pas défini. Le ksh93 est `/usr/bin/ksh`, que le
protocole n'utilise pas. Sont donc interdits, et leur absence est
vérifiée :

| Interdit | Raison |
|----------|--------|
| `[[ ]]`, tableaux, `local` | non POSIX |
| `echo -e`, `printf %q` | comportement non portable |
| substitution de processus, `read -d` | non POSIX |
| `grep -o`, `grep -P` | absents ou divergents sur AIX |

Deux particularités de ce Bourne shell ne se devinent pas à la lecture du
code, et se paient cher si on les suppose ksh93. Elles ont été mesurées sur
l'hôte, pas déduites.

### `set -u` est inutilisable

Sous `set -u`, ce shell lève une erreur `0403-041 Parameter not set.` sur
**toute** expansion d'un paramètre non défini — y compris sous la forme
`${V:=défaut}` qui devrait justement le définir, et y compris la forme
`${V-défaut}`. Il n'existe donc **aucune** forme d'expansion qui survive à
`set -u` dans ce shell ; les deux contournements habituels ont été essayés
avant d'abandonner.

Le premier `${N:-}` du corps suffisait à tuer le script, avant tout travail
utile, avec un `0403-041` sur stderr et **sans aucun bloc de résultat**. C'est
le défaut qui a fait échouer l'étape 4 sur les AIX du bank's d'essai.

Le prelude pose donc `set +u` partout, sans exception — voir
[Le mode strict](#le-mode-strict). C'est un choix délibéré : un mode strict
actif sur certains shells et inactif sur d'autres crée une famille de bogues
qui n'apparaît que sur la plateforme de production.

### Le trap de sortie ne reçoit pas le code de sortie

Dans ce Bourne shell, `$?` n'est pas mis à jour pour le trap de sortie : le
trap y trouve le statut de la **dernière commande exécutée avant le `exit`**.
Un `exit 70` y est donc annoncé `rc=0`, exactement comme une sortie normale.

Conséquence mesurée : le protocole entier repose sur `OSD_RESULT_END rc=`,
et cet anneau annonçait 0 pour tout échec. L'analyseur ne pouvait plus
distinguer une réussite d'un échec, et le rapport pouvait conclure à un
succès sur un export qui avait échoué.

Le code est donc transporté **explicitement** par `osd_exit`, qui le mémorise
avant de quitter ; le trap le restitue à `osd_finish`. Sur ksh93, `$?`
donnerait le même résultat, mais une forme unique pour tous les shells évite
d'avoir à savoir lequel est en service.
| `sed -i` | non POSIX |
| `cpio` | absent du `PATH` minimal |
| **tout `eval`** | règle absolue du projet |
| **tout Python** | les hôtes AIX n'en ont pas |

L'extraction d'un motif se fait par `osd_codes`, qui utilise `awk` — le seul
moyen POSIX d'extraire. `grep -o` et `sed` perdraient les codes
**silencieusement**, et ces codes sont ce qui distingue « pas d'espace disque »
d'« import interrompu » dans un ticket d'incident. Les perdre pour une option
non portable serait le pire compromis possible.

## Canal machine

`stdout` ne contient que du protocole :

```
OSD_RESULT_BEGIN
CLE=VALEUR            (0..n, une ligne, ordre libre)
OSD_ROWS_BEGIN        (facultatif)
<sortie brute de l'outil, verbatim>
OSD_ROWS_END
OSD_RESULT_END rc=<n>
```

Le mécanisme : le prelude fait `exec 3>&1; exec 1>&2`. Le **vrai** `stdout`
est conservé sur le descripteur 3, et `stdout` est redirigé vers `stderr`. Le
corps du script peut donc écrire librement — la sortie de l'outil Oracle, en
littéral — sans polluer le canal machine.

**Conséquence directe, et non stylistique** : tout ce qui sort entre
`OSD_ROWS_BEGIN` et `OSD_ROWS_END` doit être écrit `>&3`. Un `printf` sans
redirection atterrit dans le journal, et la liste des parties du dump revient
vide — l'échec se lit « aucun fichier produit par l'export » alors que
l'export a réussi et que le fichier existe.

C'est aussi ce qui rend la sortie **insensible à la locale** : les messages
Oracle sont traduits, on ne les analyse donc pas — on en extrait des codes.

## Fonctions du prelude

| Fonction | Rôle |
|----------|------|
| `osd_kv` | pose une paire `CLE=VALEUR` ; **refuse** les valeurs multi-lignes |
| `osd_rows_begin` / `osd_rows_end` | ouvrent et ferment le bloc de lignes |
| `osd_rows_file` | émet un fichier dans le bloc, en garantissant le saut de ligne final |
| `osd_die` | `OSD_FATAL=<message>` puis `exit <code>` |
| `osd_have` | teste la présence d'un binaire dans le `PATH` |
| `osd_codes` | extrait, déduplique et espace les codes d'erreur d'un motif |
| `osd_tmpfile` | fichier temporaire `0700`, **déclaré supprimable avant d'être rendu** |
| `osd_register_cleanup` | ajoute un fichier à la liste de suppression |
| `osd_cleanup` | supprime la liste, silencieusement |

## Le trap

`trap 'osd_finish $_osd_exit_code' 0` est enregistré **avant tout travail
utile**. Même une mort imprévue du script produit un bloc de résultat
exploitable. Sans cela l'appelant verrait un « bloc de résultat distant
incomplet » — un message qui n'apprend rien.

Le trap ne lit pas `$?` : il restitue le code que `osd_exit` a mémorisé. Ce n'est
pas une précaution de style, c'est la seule forme qui donne le vrai code sur
le Bourne shell d'AIX, dont le trap reçoit le statut de la dernière commande
exécutée et non celui du `exit` — voir
[Le trap de sortie ne reçoit pas le code de sortie](#le-trap-de-sortie-ne-re%C3%A7oit-pas-le-code-de-sortie).

Une sortie normale laisse `_osd_exit_code` à 0, ce qui est le code voulu. Toute
sortie non normale passe par `osd_exit`, y compris `osd_die` : c'est
`osd_exit "${2:-1}"`, pas un `exit` nu, qui garantit le code.

### Le mode strict

`set -u` serait utile : il transforme la lecture d'un paramètre jamais défini
en erreur franche, là où une chaîne vide laisserait passer une valeur absente
dans une commande ou dans un rapport. Il est **inutilisable** sur AIX, pour la
raison mesurée donnée plus haut ; le prelude pose donc `set +u` partout.

Le contrôle que `set -u` aurait fourni est remplacé par des contrôles
explicites : `osd_die` dès qu'un argument obligatoire est vide, ce qui produit
un code 64 et un `OSD_FATAL` nommant l'argument. Ces contrôles sont exercés par
des tests qui exécutent réellement le script.

`set -e` reste délibérément absent : le corps repose sur des échecs tolérés
(`|| true`, `osd_codes`, tests de présence), et un `-e` les transformerait en
arrêt silencieux du script.

## Le saut de ligne de tête

`osd_finish` émet un saut de ligne **inconditionnel** avant
`OSD_RESULT_END`. Un corps qui écrit sans saut de ligne final collerait sa
donnée au marqueur :

```
ORA-01017: invalid credentialOSD_RESULT_END rc=1
```

L'analyseur cherche le marqueur en tête de ligne, ne le reconnaît plus, et
l'échec est attribué au protocole alors que sa cause est une sortie sans saut
de ligne final. **Symptôme observé en exploitation** : « bloc de résultat
distant incomplet » sur un export qui, lui, avait parfaitement réussi.

Le saut de ligne superflu est sans effet : une ligne vide ne contient ni `=` ni
marqueur, donc l'analyseur l'ignore.

`osd_rows_file` traite le même piège autrement, par comparaison d'octets : un
saut de ligne ici tomberait **dans** les données.

## Codes de retour des scripts

| Code | Signification |
|------|---------------|
| 0 | succès |
| 64 | invocation incorrecte (argument manquant, action inconnue) |
| 66 | répertoire ou fichier absent |
| 127 | binaire introuvable |

Le 64 est distinct de 1 : un argument manquant doit être détecté par un contrôle
explicite, qui produit un `OSD_FATAL` nommant l'argument. Une lecture nue de
`$N` laisserait passer une chaîne vide et ferait échouer l'outil sans dire à
l'exploitant quoi corriger.

C'est pourquoi le corps lit ses arguments en `${N:-}` : cette forme fournit la
chaîne vide sur laquelle porte le contrôle. Elle reste la bonne forme quelle
que soit la politique du prelude sur `set -u` — le mode strict n'est pas la
raison de ce choix, la lisibilité du contrôle l'est.

## Nettoyage et secrets

Un parfile Data Pump porte la chaîne `userid`, donc potentiellement un mot de
passe. Il est :

1. écrit en `0600` ;
2. déclaré supprimable **avant** d'être nommé par le corps ;
3. supprimé par le trap, **y compris quand le corps meurt**.

D'où la règle : `osd_register_cleanup` précède *tout* `osd_die`. Un fichier
temporaire qui contient un secret ne doit pas pouvoir survivre à une erreur.

## Motif d'énumération

`remote_listdir.sh` sert à deux choses, et son motif est traité différemment
selon l'action :

- **`list`** — le motif est exigé et **ancré** (`préfixe` + `suffixe`). Un
  motif vide listerait tous les `.dmp` du répertoire, et attribuerait au run en
  cours les fichiers d'un autre run — donc les transférerait, puis les
  supprimerait au nettoyage.
- **`unlink`** — le motif est **inconnu et sans objet** : la suppression reçoit
  des noms déjà connus. L'usage documenté est `'' '' unlink <noms...>`, et
  l'exiger malgré tout condamnait tout le nettoyage.

Le motif est développé par le shell (`"$dir"/"$prefix"*"$suffix"`), pas par
`find` : `find` descend dans les sous-répertoires, et un fichier portant le nom
d'une partie, logé dans un sous-répertoire du DIRECTORY, serait compté comme
une partie du dump. Le glob ne descend jamais.

## Ce que le protocole garantit

- `stdout` ne contient jamais de texte libre — vérifié par un test qui parcourt
  la sortie ligne à ligne ;
- les données ne se perdent pas dans le journal — le symétrique, vérifié par
  la présence effective des lignes ;
- un corps qui sort en erreur livre quand même un bloc complet ;
- la valeur multi-ligne est **refusée**, pas tronquée : une clé coupée en deux
  donnerait deux entrées fausses, dont une muette.

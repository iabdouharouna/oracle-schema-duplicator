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

- **avant `set -u`** — un profil ne déclare pas toujours les variables
  qu'il initialise ; sourcé après, le script meurt avant le prelude, donc
  avant tout résultat : un échec muet, sans bloc à analyser ni à rapporter.
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
  évalue `osd_parpath: unbound variable` sous `set -u` et meurt avant l'export.

`bootstrap` ne peut donc pas référencer `osd_argN`, et aucun appelant n'en a
besoin.

Les variables `osd_argN` sont recombinées par `set --` pour que le corps
utilise `"$@"` comme un script normal. `Raw` est réservé aux valeurs déjà
définies ; tout le reste est mis entre guillemets simples.

## Contraintes du shell

Sur AIX, `/bin/sh` est **ksh93**. Sont donc interdits, et leur absence est
vérifiée :

| Interdit | Raison |
|----------|--------|
| `[[ ]]`, tableaux, `local` | non POSIX |
| `echo -e`, `printf %q` | comportement non portable |
| substitution de processus, `read -d` | non POSIX |
| `grep -o`, `grep -P` | absents ou divergents sur AIX |
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

`trap 'osd_finish $?' 0` est enregistré **avant tout travail utile**. Même un
`set -u` qui échoue sur une variable non définie produit un bloc de résultat
exploitable, avec `rc != 0`. Sans cela l'appelant verrait un « bloc de résultat
distant incomplet » — un message qui n'apprend rien.

`$?` est développé **avant** l'appel, donc il vaut le code qui déclenche le trap
et non celui du `rm` de nettoyage.

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

Le 64 est distinct de 1 : sous `set -u`, un argument manquant arrêterait le
script sur « unbound variable » avec un code 1, indistinct d'une erreur interne
du shell et sans `OSD_FATAL`. L'appelant ne pourrait ni distinguer les deux, ni
dire à l'exploitant quoi corriger.

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

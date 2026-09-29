# Protocole d'exécution distante

Ce document décrit le contrat entre le serveur de saut et l'hôte qui porte la
base. Il est implémenté par `shell/prelude.sh` et `src/osd/runner.py`, et
vérifié par `tests/unit/test_protocol.py`.

## Transport

Le script complet part sur `stdin` :

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 <hote> sh -s
```

`BatchMode=yes` est **exigé** par la validation. Sans lui, une erreur
d'authentification ouvre une invite interactive et le run reste bloqué jusqu'à
l'expiration du crontab.

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

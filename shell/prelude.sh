#!/bin/sh
# OSD prelude — contrat d'execution distante.
#
# Ce fichier est concatene par le Python a un corps de script
# (shell/remote_*.sh) puis envoye sur stdin via :
#
#     ssh -o BatchMode=yes <hote> sh -s
#
# Contraintes strictes, non negociables :
#   * POSIX sh uniquement, et **Bourne shell d'AIX compris**. Sur AIX 7.2,
#     `/bin/sh` est le Bourne shell (`KSH_VERSION` n'y est pas defini) :
#     ni `[[ ]]`, ni tableaux, ni `local`, ni `echo -e`, ni `printf %q`,
#     ni substitution de processus, ni `read -d`.
#   * Deux peculiarities de ce Bourne shell sont traitees plus bas, et
#     aucune ne se devine a la lecture du code : `set -u` y rend ILLICITE
#     toute expansion d'un parametre non defini, et le trap de sortie n'y
#     recoit pas le code de sortie (voir `set +u` et `osd_exit`). Les
#     traiter comme si le shell etait ksh93 a produit des scripts qui
#     mouraient a la premiere ligne, avec un code 0 et aucun diagnostic
#     exploitable — le pire des deux mondes.
#   * Aucun Python : les hotes AIX n'en ont pas.
#   * Aucun `eval`, quelle que soit la forme des arguments.
#   * Aucun `grep -o`, `grep -P`, `sed -i`, `local` : absents ou
#     divergents sur AIX. Voir `osd_codes` pour l'extraction
#     d'un motif en POSIX.
#
# Protocole de sortie (stdout, cf. docs/REMOTE_PROTOCOL.md) :
#
#   OSD_RESULT_BEGIN
#   CLE=VALEUR            (0..n, une ligne, ordre libre)
#   OSD_ROWS_BEGIN        (facultatif)
#   <sortie brute de l'outil, verbatim>
#   OSD_ROWS_END
#   OSD_RESULT_END rc=<n>
#
# La sortie de travail est redirigee vers stderr : stdout ne contient
# donc JAMAIS de texte libre, ce qui rend l'analyse deterministe et
# insensible a la locale de l'hote (les messages Oracle sont traduits).
#
# Le trap sur le pseudo-descripteur 0 est enregistre avant tout travail
# utile : meme si le script meurt, il produit un bloc de resultat
# exploitable.

# --- mode strict ----------------------------------------------------------
# `set -u` est desirable : il transforme la lecture d'un parametre jamais
# defini en erreur franche, la ou une chaine vide laisserait passer une
# valeur absente dans une commande ou dans un rapport.
#
# Il est cependant **inutilisable** dans le Bourne shell d'AIX, que
# `/bin/sh` designe aussi (`/bin/sh` et `/usr/bin/sh` y sont le meme
# binaire, et `KSH_VERSION` n'y est pas defini). Dans ce shell, `set -u`
# rend ILLICITE toute expansion d'un parametre non defini, y compris sous
# la forme `${V:=defaut}` qui devrait justement le definir. Le premier
# `${N:-}` du corps arretait alors tout le script avec
#
#     0403-041 Parameter not set.
#
# Deux contournements ont ete mesures sur l'hote avant d'abandonner
# `set -u` : remplacer `${V:-d}` par `${V:=d}` echoue aussi, et il n'existe
# donc AUCUNE forme d'expansion qui survive a `set -u` dans ce shell.
#
# On pose donc `set +u` partout, sans exception. C'est un choix, pas une
# adaptation opportuniste : un mode strict actif sur certains shells et
# inactif sur d'autres creerait une famille de bogues qui n'apparait que sur
# la plateforme de production. Le strict est remplace par des controles
# explicites — `osd_die` des que la valeur d'un argument est vide — dont
# l'effet est deja verifie par les tests unitaires.
set +u

# `set -e` reste deliberement absent : le corps repose sur des echecs
# toleres (`|| true`, `osd_codes`, tests de presence), et un `-e` les
# transformerait en arret silencieux du script.

# stdout reel conserve sur le descripteur 3, puis stdout redirige vers
# stderr : le corps du script peut ecrire librement, sans polluer le
# canal machine.
exec 3>&1
exec 1>&2

#: Fichiers a supprimer en sortie, un par ligne.
#:
#: Volontairement vide au depart : un script qui n'a cree aucun fichier
#: temporaire n'a rien a nettoyer. L'initialisation reste neanmoins
#: necessaire, `osd_register_cleanup` concatene sans tester -- mais par
#: Constance, non par `set -u`, que ce shell ne pose plus.
_osd_cleanup_list=''

osd_register_cleanup() {
    # Declare un fichier a supprimer en sortie.
    #
    # Reserve au fichier produit par un `bootstrap` (le parfile Data
    # Pump) : les fichiers temporaires du script sont, eux, deja
    # enregistres par `osd_tmpfile`.
    #
    # La liste est une chaine et non un tableau : aucun `local` n'etant
    # disponible en POSIX sh, on evite des expansions risquées.
    _osd_cleanup_list="${_osd_cleanup_list}
$1"
}

osd_cleanup() {
    # Supprime tous les fichiers declares. Silencieux : un fichier deja
    # absent, ou insupprimable, ne doit pas masquer le resultat du
    # script, qui reste l'information utile.
    #
    # La lecture se fait par `while read` et non par `for ... in`, qui
    # scinderait sur les blancs : un `TMPDIR` contenant un espace
    # produirait alors une suppression sur un chemin qui n'existe pas,
    # et laisserait le vrai fichier derriere.
    printf '%s\n' "$_osd_cleanup_list" | while read -r _osd_f; do
        [ -n "$_osd_f" ] || continue
        rm -f "$_osd_f" 2>/dev/null || true
    done
}

osd_finish() {
    # Emet le bloc final. $1 = code de sortie du script.
    #
    # Le nettoyage precede l'emission : peu importe l'ordre, mais si le
    # `rm` levait une erreur bloquante, mieux vaut que le marqueur de
    # fin soit deja parti. Dans les faits `rm` est en erreur t tolerated.
    osd_cleanup
    #
    # Le saut de ligne de tete est **inconditionnel**, et c'est
    # volontaire. Sans lui, un corps qui ecrit sans saut de ligne final
    # — `cat` d'un fichier dont la derniere ligne n'est pas terminee,
    # une commande dont la sortie n'en a pas — colle sa donnee au
    # marqueur. Le bloc devient alors illisible cote Python, qui
    # cherche `OSD_RESULT_END` en tete de ligne :
    #
    #     OSD_RESULT_BEGIN
    #     ORA-01017: invalid credentialOSD_RESULT_END rc=0
    #
    # et l'echec est attribue au protocole alors que sa cause est une
    # sortie sans saut de ligne final. Symptome observe en exploitation :
    # « bloc de resultat distant incomplet » sur un export qui, lui, avait
    # parfaitement reussi.
    #
    # Le saut de ligne superflu est sans effet : une ligne vide ne
    # contient ni `=` ni marqueur, donc `_parse_result` l'ignore. Il
    # rend en revanche la structure du bloc garantie, quel que soit
    # l'etat du flux quand le trap s'execute.
    printf '\nOSD_RESULT_END rc=%s\n' "$1" >&3 2>/dev/null || true
}

osd_kv() {
    # Enregistre une paire clef/valeur dans le bloc de resultat.
    # Les valeurs multilignes sont refusees ici plutot que de produire un
    # bloc invalide cote Python.
    case "$2" in
        *"
"*) printf 'OSD_KV_ERROR=multiline key=%s\n' "$1" >&3 2>/dev/null || true ;;
        *) printf '%s=%s\n' "$1" "$2" >&3 2>/dev/null || true ;;
    esac
}

osd_rows_begin() { printf 'OSD_ROWS_BEGIN\n' >&3 2>/dev/null || true; }
osd_rows_end()   { printf 'OSD_ROWS_END\n'   >&3 2>/dev/null || true; }

# Emet le contenu d'un fichier dans le bloc de lignes, en garantissant
# que la derniere ligne est terminee.
#
# Un `cat` nu ne suffit pas : une sortie qui ne se termine pas par un
# saut de ligne — `printf 'texte'`, un `tail -c` — colle sa donnee au
# marqueur `OSD_ROWS_END`. L'analyseur verifie le marqueur par egalite
# de ligne, il ne le reconnait donc plus, et le bloc reste ouvert : la
# derniere ligne est rendue comme une donnee, avec le marqueur colle a
# elle. Un nom de fichier se retrouve alors dans le rapport.
#
# C'est le meme piege que `osd_finish` resout en emettant un saut de
# ligne avant son marqueur, mais ici la ligne vide supplementaire
# tomberait **dans** les donnees. D'ou une comparaison d'octets plutot
# qu'un saut de ligne systematique : elle n'ajoute rien quand la sortie
# est deja correctement terminee.
osd_rows_file() {
    osd_rf=$1
    [ -f "$osd_rf" ] || return 0
    [ -s "$osd_rf" ] || return 0
    cat "$osd_rf" >&3 2>/dev/null || true
    # Derniere question : le fichier se termine-t-il par un saut de
    # ligne ? Une substitution de commande **absorbe les sauts de ligne
    # finaux**, donc une valeur vide signifie exactement « oui » — le
    # fichier etant non vide, aucune autre lecture n'est possible.
    # Comparer le nombre d'octets a celui des sauts de ligne ne
    # conviendrait pas : un fichier correctement termine en contient
    # moins, et une ligne vide parasite serait alors ajoutee aux
    # donnees.
    osd_rf_dernier=$(tail -c 1 "$osd_rf" 2>/dev/null || true)
    if [ -n "$osd_rf_dernier" ]; then
        printf '\n' >&3 2>/dev/null || true
    fi
}

osd_die() {
    # $1 = message, $2 = code de sortie
    printf 'OSD_FATAL=%s\n' "$1" >&3 2>/dev/null || true
    osd_exit "${2:-1}"
}

osd_have() {
    # $1 = commande ; vrai si disponible dans le PATH.
    command -v "$1" >/dev/null 2>&1
}

osd_codes() {
    # Codes d'erreur presents sur l'entree standard, dedupliques et
    # espaces, prets a etre poses par `osd_kv`.
    #   $1 = motif, une ERE (une seule)
    #
    # L'appelant fait `cat ... | osd_codes 'motif'`. Lire l'entree
    # standard plutot qu'un chemin evite `/dev/stdin`, dont l'existence
    # varie, et evite un fichier temporaire de plus sur un hote ou
    # `/tmp` est surveille.
    #
    # L'implementation est en `awk` pour une raison precise : `match()`
    # est le seul moyen **POSIX** d'extraire un motif. `grep -o` n'est
    # pas POSIX et manque sur certaines versions du `grep` d'AIX ;
    # `sed` n'a pas d'equivalent, et son `\|` d'alternance n'est pas
    # non plus garanti. Avec le `|| true` qui avale l'echec, ces deux
    # solutions perdraient les codes **silencieusement** — or ces codes
    # sont ce qui distingue « pas d'espace disque » d'« import
    # interrompu » dans un ticket d'incident. Les perdre pour une option
    # non portable serait le pire compromis possible.
    #
    # `awk` est present sur AIX (`/usr/bin/awk`), comme le sont deja
    # `tr`, `sed` et `sort` utilises ci-dessous.
    #
    # La boucle `while match(...)` traite plusieurs occurrences sur une
    # meme ligne, cas reellement rencontre : un message Oracle en
    # contient souvent deux. `RLENGTH` a 0 ne peut pas boucler a
    # l'infini car il est reclame par le corps de la boucle.
    awk -v motif="$1" '
        {
            reste = $0
            while (match(reste, motif)) {
                print substr(reste, RSTART, RLENGTH)
                reste = substr(reste, RSTART + RLENGTH)
                if (RLENGTH == 0) break
            }
        }
    ' 2>/dev/null \
        | sort -u \
        | tr '\n' ' ' \
        | sed 's/^ *//; s/ *$//'
}

osd_tmpfile() {
    # Fichier temporaire lisible uniquement par l'utilisateur courant.
    # mktemp est POSIX mais pas toujours present sur AIX : on garde un
    # repli base sur le PID.
    #
    # Le fichier est declare supprimable avant d'etre rendu : un caller
    # qui mourrait entre les deux laissait un fichier contenant
    # potentiellement un `userid` ou du SQL, lisible par tout compte de
    # l'hote.
    _osd_t="${TMPDIR:-/tmp}/osd.$$.${1:-tmp}"
    if (umask 077 && : > "$_osd_t") 2>/dev/null; then
        osd_register_cleanup "$_osd_t"
        printf '%s\n' "$_osd_t"
        return 0
    fi
    return 1
}

# --- transport du code de sortie -------------------------------------------
# Le trap doit annoncer le code REEL, et non `$?`.
#
# Mesure sur AIX 7.2 (Bourne shell), le trap de sortie ne recoit pas le
# code du `exit` : il y trouve le statut de la derniere commande executee
# avant lui. Un `exit 70` y est donc annonce `rc=0`, comme une sortie
# normale. L'analyseur, qui tire son verdict de `OSD_RESULT_END rc=`,
# ne pouvait alors plus distinguer un succes d'un echec — et le protocole
# repose entierement sur ce marqueur.
#
# Le code voyage donc explicitement : `osd_exit` le memorise avant de
# quitter, et le trap le restitue. Sur ksh93, `$?` donnerait le meme
# resultat, mais une seule forme pour tous les shells evite d'avoir a
# savoir lequel est en service.
#
# Le code **processus**, lui, n'est que du transport — et sous Ansible
# il est lu par `sshpass`, avant meme que le bloc existe. Deux valeurs
# y sont fatales :
#
# * 5 : sshpass renvoie son code propre quand il reussit lui-meme, et
#   Ansible lit alors 5 comme « mot de passe incorrect » (`_handle_error`
#   du plugin connection/ssh). La tache est declaree injoignable, la
#   sortie complete est jetee sans un mot et sans retry, et l'echec se
#   conclut sur un diagnostic muet sans rapport avec la cause. Or
#   `impdp` sort 5 quand le job aboutit avec des erreurs — le cas
#   normal d'un import `TABLE_EXISTS_ACTION=SKIP` sur un schema deja
#   peuple, ou chaque objet non-table deja present est signale
#   `ORA-31684`.
# * 255 : le meme plugin y lit « la connexion ssh a echoue », rejoue la
#   connexion puis echoue — un code qu'un client courant peut sortir
#   de facon anodine.
#
# Ces deux valeurs sont donc deviees, vers des codes sans signification
# dans tout le projet : 71 et 72 evitent 5 comme 255, et ne sont emis
# par aucun autre chemin interne (`osd_die` use de 64, 65, 66, 69, 70,
# 73 et 127). La valeur exacte n'a aucune importance : le code reel
# reste dans `_osd_exit_code`, que le trap ecrit dans
# `OSD_RESULT_END rc=`, substitue au code processus par
# `_parse_result`. Le bloc, et lui seul, distingue donc un 71 arrive de
# facon legitime d'un 5 devie — le projet ne lit jamais le code
# processus, et la seule valeur qui change est celle qu'Ansible
# interpretait a tort.
_osd_exit_code=0
osd_exit() {
    # $1 = code de sortie du script
    _osd_exit_code="$1"
    case "$1" in
        5)   exit 71 ;;
        255) exit 72 ;;
    esac
    exit "$1"
}
trap 'osd_finish $_osd_exit_code' 0

printf 'OSD_RESULT_BEGIN\n' >&3 2>/dev/null || true

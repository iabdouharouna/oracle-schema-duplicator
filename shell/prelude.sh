#!/bin/sh
# OSD prelude — contrat d'execution distante.
#
# Ce fichier est concatene par le Python a un corps de script
# (shell/remote_*.sh) puis envoye sur stdin via :
#
#     ssh -o BatchMode=yes <hote> sh -s
#
# Contraintes strictes, non negociables :
#   * POSIX sh uniquement. Sur AIX, /bin/sh est ksh93 : pas de [[ ]],
#     pas de tableaux, pas de `local`, pas de `echo -e`, pas de
#     `printf %q`, pas de substitution de processus, pas de `read -d`.
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
# utile : m^eme un `set -u` qui echoue sur une variable non definie
# produit un bloc de resultat exploitable, avec rc != 0.

set -u

# stdout reel conserve sur le descripteur 3, puis stdout redirige vers
# stderr : le corps du script peut ecrire librement, sans polluer le
# canal machine.
exec 3>&1
exec 1>&2

#: Fichiers a supprimer en sortie, un par ligne. Volontairement vide au
#: depart : `set -u` impose de l'initialiser, sinon la premiere lecture
#: dans `osd_cleanup` echouerait sur un script qui n'a cree aucun
#: fichier temporaire.
_osd_cleanup_list=''

osd_register_cleanup() {
    # Declare un fichier a supprimer en sortie.
    #
    # Reserve au fichier produit par un `bootstrap` (le parfile Data
    # Pump) : les fichiers temporaires du script sont, eux, deja
    # enregistres par `osd_tmpfile`.
    #
    # La liste est une chaine et non un tableau, `set -u` etant actif et
    # aucun `local` n'etant disponible en POSIX sh.
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
    exit "$2"
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

# `$?` est developpe **avant** l'appel, donc il vaut bien le code de
# sortie qui declenche le trap, et non celui du `rm` de nettoyage. Le
# statut du script reste donc celui de `exit`, et non 0.
trap 'osd_finish $?' 0

printf 'OSD_RESULT_BEGIN\n' >&3 2>/dev/null || true

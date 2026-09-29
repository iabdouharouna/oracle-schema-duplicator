# OSD remote_pathinfo — ecrire, verifier, mesurer, supprimer un fichier.
#
# Corps POSIX sh utilise par la sonde de capacite du transfert
# (adapters/transfer.py). Une seule commande, quatre operations, pour que
# la sonde reste un seul aller-retour.
#
# Usage :
#   remote_pathinfo write  <repertoire> <nom>
#   remote_pathinfo verify <repertoire> <nom>
#   remote_pathinfo size   <repertoire> <nom>
#   remote_pathinfo remove <repertoire> <nom>
#
# Le contenu du temoin est fourni par l'amorcage, dans la variable
# `osd_probecontent`. Ce n'est pas un parametre : il peut contenir des
# caracteres arbitraires, et le faire voyager par `"$@"` obligerait a
# supposer qu'il n'en contient pas.
#
# `verify` compare le contenu octet a octet, et non seulement la
# presence du fichier : une copie tronquee doit etre detectee, ce qu'un
# simple `test -f` ne ferait pas.

# `${N:-}` et non `$N` : sous le `set -u` du prelude, un argument
# manquant arreterait le script sur « unbound variable » avant tout
# controle, avec un code 1 indistinct d'une erreur interne et sans
# `OSD_FATAL`. Le code 64, lui, signifie « invocation incorrecte » et
# se distingue des autres.
osd_op=${1:-}
osd_dir=${2:-}
osd_name=${3:-}

if [ -z "$osd_op" ] || [ -z "$osd_dir" ] || [ -z "$osd_name" ]; then
    osd_die "remote_pathinfo: arguments incomplets" 64
fi

# Un nom de fichier contenant `/` ou `..` permettrait d'ecrire hors du
# repertoire de travail. Les noms produits par l'outil sont des noms de
# fichiers Data Pump, donc sans separateur ; on refuse donc explicitement
# le cas general plutot que de s'y fier.
case "$osd_name" in
    */*|..|./*) osd_die "nom de fichier invalide: $osd_name" 64 ;;
esac

osd_target=$osd_dir/$osd_name

case "$osd_op" in
write)
    if [ ! -d "$osd_dir" ]; then
        osd_kv OSD_DIR_MISSING "$osd_dir"
        osd_die "repertoire inexistant: $osd_dir" 66
    fi
    if [ -z "${osd_probecontent+x}" ]; then
        osd_die "contenu du temoin absent" 64
    fi
    # umask 077 : le temoin ne doit jamais etre lisible par un tiers,
    # meme le temps de sa breve existence.
    (umask 077 && printf %s "$osd_probecontent" > "$osd_target") 2>/dev/null \
        || osd_die "ecriture impossible dans $osd_dir" 73
    osd_kv OSD_WRITTEN "$osd_target"
    ;;

verify)
    if [ ! -f "$osd_target" ]; then
        osd_kv OSD_PRESENT 0
        osd_die "fichier absent apres transfert: $osd_target" 66
    fi
    osd_kv OSD_PRESENT 1
    if [ -n "${osd_probecontent+x}" ]; then
        if printf %s "$osd_probecontent" | cmp -s - "$osd_target" 2>/dev/null; then
            osd_kv OSD_INTACT 1
        else
            osd_kv OSD_INTACT 0
            # `cmp` est POSIX ; s'il est absent, la comparaison echoue et
            # l'echec est traite comme une alteration, ce qui est le
            # comportement prudent.
            osd_die "fichier altere apres transfert" 65
        fi
    fi
    ;;

size)
    if [ -f "$osd_target" ]; then
        osd_kv OSD_SIZE "$(wc -c < "$osd_target" | tr -d ' ')"
    else
        osd_kv OSD_SIZE 0
    fi
    ;;

remove)
    # Le nettoyage ne doit jamais faire echouer la sonde : un fichier deja
    # absent est un etat acceptable, pas une erreur.
    rm -f "$osd_target" 2>/dev/null || true
    osd_kv OSD_REMOVED "$osd_target"
    ;;

*)
    osd_die "operation inconnue: $osd_op" 64
    ;;
esac

exit 0

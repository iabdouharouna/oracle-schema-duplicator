# OSD remote_listdir — enumeration et suppression dans un repertoire.
#
# Deux usages distincts, un seul corps de script :
#
#   1. **enumeration** — decouvrir les fichiers reellement produits par
#      un export. Les noms ne sont pas devinables : avec `PARALLEL > 1`,
#      Data Pump choisit le nombre de parties selon la volumetrie, et le
#      jeton `%d` ne garantit pas une serie complete. C'est la seule
#      approche fiable, et elle est aussi la seule qui detecte une partie
#      manquante.
#
#   2. **suppression** — nettoyer le dump apres un import reussi.
#
# Usage :
#   remote_listdir <repertoire> <prefixe> <suffixe> [list]
#   remote_listdir <repertoire> '' '' unlink <nom1> <nom2> ...
#
# Le motif est anchoré (`prefixe` + `suffixe`), jamais une recherche
# large : un run concurrent ne doit pas pouvoir faire disparaitre les
# fichiers d'un autre run, ni les faire passer pour les siens.

osd_dir=${1:-}
osd_prefix=${2:-}
osd_suffix=${3:-}
# `${N:-}` et non `$N` : sous le `set -u` du prelude, un argument
# manquant arreterait le script sur « unbound variable » avant tout
# controle, avec un code 1 indistinct d'une erreur interne et sans
# `OSD_FATAL`.
if [ -z "$osd_dir" ]; then
    osd_die "remote_listdir: aucun repertoire fourni" 64
fi
shift 3
osd_action=${1:-list}
if [ "$#" -gt 0 ]; then
    shift
fi

# Le motif n'est exige que pour `list`, jamais pour `unlink` : la
# suppression recoit des noms deja connus, et l'usage documente
# (`'' '' unlink ...`) les laisse vides. Les exiger malgre tout
# condamnait le nettoyage — l'etape 18 ne supprimait plus rien et
# annoncait « 0 artefact(s) supprime(s) », sans erreur ni trace.
#
# Les exiger pour `list`, en revanche, est indispensable : un prefixe
# ou un suffixe vide transformerait l'inventaire en « tous les `.dmp`
# du repertoire », attribuant au run les fichiers d'un autre run.
if [ "$osd_action" = list ]; then
    if [ -z "$osd_prefix" ] || [ -z "$osd_suffix" ]; then
        osd_die "remote_listdir: motif incomplet pour list" 64
    fi
fi

if [ ! -d "$osd_dir" ]; then
    osd_die "repertoire inexistant: $osd_dir" 66
fi

case "$osd_action" in
list)
    osd_rows_begin
    # Le motif est developpe **par le shell**, et non par `find`.
    #
    # `find` descend dans les sous-repertoires : un fichier portant le
    # nom d'une partie, loge dans un sous-repertoire du DIRECTORY, etait
    # alors rendu comme une partie du dump — donc transfere a l'etape
    # 13, puis supprime au nettoyage. L'ancrage du motif ne l'empeche
    # pas, contrairement a ce que la premiere version de ce script
    # supposait. `find -maxdepth` serait la reponse habituelle, mais
    # l'option n'est pas POSIX ; le glob, lui, ne descend jamais.
    #
    # Le prefixe est cite pour rester litteral : un caractere de glob
    # qu'il contiendrait decouvrirait des fichiers sans rapport. Le
    # suffixe est laisse libre, c'est lui qui porte le motif.
    for osd_hit in "$osd_dir"/"$osd_prefix"*"$osd_suffix"; do
        [ -f "$osd_hit" ] || continue
        # Le nom seul est renvoye, sans le chemin complet : c'est ce que
        # l'appelant doit reutiliser, et cela evite de propager une
        # chaine contenant des espaces.
        #
        # `>&3` est obligatoire, et non stylistique : le prelude a
        # redirige la sortie standard vers la sortie d'erreur, le canal
        # machine etant conserve sur le descripteur 3. Un `printf` sans
        # redirection atterrit donc dans le journal et la liste des
        # parties du dump revient vide — l'echec se lisait « aucun
        # fichier produit par l'export » alors que l'export avait reussi
        # et que le fichier existait.
        osd_base=${osd_hit##*/}
        printf '%s\n' "$osd_base" >&3 2>/dev/null || true
    done
    osd_rows_end
    osd_kv OSD_LISTED "$osd_dir"
    ;;

unlink)
    osd_removed=0
    for osd_name in "$@"; do
        [ -n "$osd_name" ] || continue
        # Garde-fou identique a remote_pathinfo : un nom contenant `/`
        # viserait un autre repertoire que celui demande.
        case "$osd_name" in
            */*|..) continue ;;
        esac
        if [ -f "$osd_dir/$osd_name" ]; then
            rm -f "$osd_dir/$osd_name" 2>/dev/null && osd_removed=$((osd_removed + 1))
        fi
    done
    osd_kv OSD_REMOVED "$osd_removed"
    ;;

*)
    # Une action inconnue ne doit pas se comporter comme un `list` sans
    # motif, c'est-a-dire comme un inventaire de tout le repertoire.
    osd_die "remote_listdir: action inconnue: $osd_action" 64
    ;;
esac

exit 0

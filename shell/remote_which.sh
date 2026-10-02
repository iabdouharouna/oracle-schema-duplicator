# OSD remote_which — un binaire est-il dans le PATH de l'hote ?
#
# Corpus de la verification de dependances (etape 3 du workflow) et de
# la sonde de capacite du transfert.
#
# Usage :
#   remote_which <commande>
#
# Le resultat est porte par `OSD_FOUND`, pas par le code de sortie seul :
# un code 1 est ambigu (commande absente, ou erreur interne du shell),
# la valeur de `OSD_FOUND` ne l'est pas.

osd_bin=${1:-}
if [ -z "$osd_bin" ]; then
    osd_die "remote_which: aucun binaire fourni" 64
fi

osd_path=$(command -v "$osd_bin" 2>/dev/null || true)

if [ -n "$osd_path" ]; then
    osd_kv OSD_FOUND 1
    osd_kv OSD_PATH "$osd_path"
    osd_exit 0
fi

osd_kv OSD_FOUND 0
osd_die "binaire absent du PATH: $osd_bin" 127

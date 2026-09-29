# OSD remote_exec — execution generique d'une commande distante.
#
# Corps de script, a concatener apres prelude.sh. Les arguments sont
# injectes par le Python sous forme de variables `osd_argN` mises entre
# simples quotes, puis recombines par `set --`. Le corps les recoit donc
# dans "$@" sans aucun quoting ni aucune possibilite d'injection.
#
# Ce script sert aussi de modele : tout nouveau remote_*.sh s'execute
# dans ce meme cadre.
#
# Usage :
#   remote_exec <commande> [args...]
#
# Exemple :
#   remote_exec ls -l /data/oracle/backup/export

osd_cmd=${1:-}
if [ -z "$osd_cmd" ]; then
    osd_die "remote_exec: aucune commande fournie" 64
fi
shift

# Chemin du binaire resolu cote hote, pour un message d'erreur clair.
osd_path=$(command -v "$osd_cmd" 2>/dev/null || true)
if [ -z "$osd_path" ]; then
    osd_kv OSD_MISSING_CMD "$osd_cmd"
    osd_die "commande introuvable sur l'hote: $osd_cmd" 127
fi
osd_kv OSD_EXEC_PATH "$osd_path"

# stdout de la commande : on le veut dans le bloc de resultat, donc on
# capture stdout et stderr separement.
osd_out=$(osd_tmpfile out) || osd_die "fichier temporaire impossible" 70
osd_err=$(osd_tmpfile err) || osd_die "fichier temporaire impossible" 70

"$osd_path" "$@" >"$osd_out" 2>"$osd_err"
osd_rc=$?

osd_kv OSD_RC "$osd_rc"
osd_kv OSD_STDOUT_BYTES "$(wc -c <"$osd_out" | tr -d ' ')"
osd_kv OSD_STDERR_BYTES "$(wc -c <"$osd_err" | tr -d ' ')"

if [ -s "$osd_out" ]; then
    osd_rows_begin
    # `osd_rows_file`, et non `cat` : une commande dont la sortie ne se
    # termine pas par un saut de ligne collerait sa derniere ligne au
    # marqueur de fin, que l'analyseur reconnait par egalite de ligne.
    osd_rows_file "$osd_out"
    osd_rows_end
fi

# La sortie d'erreur est recopiee sur stderr du shell parent, donc
# directement visible dans les journaux, sans polluer le bloc machine.
if [ -s "$osd_err" ]; then
    sed 's/^/  [remote] /' "$osd_err" >&2 2>/dev/null || true
fi

rm -f "$osd_out" "$osd_err" 2>/dev/null || true
exit "$osd_rc"

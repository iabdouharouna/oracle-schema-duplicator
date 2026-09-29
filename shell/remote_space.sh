# OSD remote_space — espace disque disponible sur un chemin donne.
#
# Etape 9 du workflow. L'espace d'un DIRECTORY Oracle n'est pas
# interrogeable en SQL : seule la place libre dans les TABLESPACES
# l'est (via DBA_FREE_SPACE). Pour le disque du serveur, il faut le
# systeme de fichiers, donc le shell — et c'est ici que l'on evite
# d'ecrire du code non portable.
#
# Portable AIX/POSIX :
#   * `df -k` : POSIX, blocs de 1024 octets, present sur AIX et Linux.
#     `df -Pk` est la forme strictement portable mais n'est pas
#     universellement acceptee ; on evite donc de s'y fier et on lit la
#     derniere ligne.
#   * pas de `stat -c` (GNU), pas de `du -sb` (GNU).
#   * awk POSIX uniquement, avec intervalles `\t` toleres.
#
# Usage :
#   remote_space <chemin_absolu>

osd_path=${1:-}
if [ -z "$osd_path" ]; then
    osd_die "remote_space: aucun chemin fourni" 64
fi

if ! osd_have df; then
    osd_kv OSD_MISSING_CMD df
    osd_die "df absent du PATH sur cet hote" 127
fi

# Nom de fichier ou de repertoire : un chemin se terminant par `/` est
# traite comme un repertoire par `df` sur les deux systemes.
if [ ! -e "$osd_path" ]; then
    osd_kv OSD_PATH "$osd_path"
    osd_kv OSD_EXISTS 0
    osd_die "chemin absent sur l'hote: $osd_path" 66
fi

if [ -d "$osd_path" ]; then
    osd_target="$osd_path"
else
    # Pour un fichier, on interroge le repertoire qui le contient, afin
    # d'obtenir la place du systeme de fichiers et non celle du fichier.
    osd_target=$(dirname "$osd_path")
fi

osd_kv OSD_PATH "$osd_path"
osd_kv OSD_EXISTS 1
osd_kv OSD_FILESYSTEM "$osd_target"

# Sortie de df -k : 6 colonnes POSIX (Filesystem, 1024-blocks, Used,
# Available, Capacity, Mounted on). Certaines variantes AIX placent le
# point de montage avant le nom de peripherie, d'ou la selection par
# position et non par nom.
osd_df=$(df -k "$osd_target" 2>/dev/null | tail -n 1)
if [ -z "$osd_df" ]; then
    osd_die "df n'a pas pu analyser $osd_target" 69
fi

osd_kv OSD_DF_RAW "$osd_df"

osd_total_kb=$(printf '%s\n' "$osd_df" | awk '{ print $2 }')
osd_used_kb=$(printf '%s\n' "$osd_df" | awk '{ print $3 }')
osd_avail_kb=$(printf '%s\n' "$osd_df" | awk '{ print $4 }')

# Validation sans `eval` (interdit par AGENTS.md) : une valeur de `df`
# inexploitable doit degrader l'etape 9 en erreur explicite plutot que
# produire un calcul faux silencieusement.
osd_validate_kb() {
    case "$2" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

if ! osd_validate_kb x "$osd_total_kb" || ! osd_validate_kb x "$osd_used_kb" \
   || ! osd_validate_kb x "$osd_avail_kb"; then
    osd_die "sortie de df illisible pour $osd_target" 69
fi

osd_kv OSD_TOTAL_KB "$osd_total_kb"
osd_kv OSD_USED_KB "$osd_used_kb"
osd_kv OSD_AVAIL_KB "$osd_avail_kb"
osd_kv OSD_TOTAL_BYTES "$((osd_total_kb * 1024))"
osd_kv OSD_USED_BYTES "$((osd_used_kb * 1024))"
osd_kv OSD_AVAIL_BYTES "$((osd_avail_kb * 1024))"

# Pourcentage d'occupation, calcule sans dependre du champ Capacity,
# dont le format varie ("42%" ou "42% / 1000 blocs" sur certaines
# implementations AIX).
if [ "$osd_total_kb" -gt 0 ] 2>/dev/null; then
    osd_pct=$(( (osd_used_kb * 100) / osd_total_kb ))
    osd_kv OSD_USED_PERCENT "$osd_pct"
fi

exit 0

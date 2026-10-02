#!/bin/sh
# OSD remote_datapump — execution d'expdp / impdp sur un hote Oracle.
#
# Corps POSIX sh, execute par le Runner dans le cadre du prelude. Les
# arguments arrivent par "$@" (voir docs/REMOTE_PROTOCOL.md).
#
# Usage :
#   remote_datapump expdp <parfile_absolu> <timeout_secondes>
#   remote_datapump impdp <parfile_absolu> <timeout_secondes>
#
# Le parfile est produit par le Python et ecrit en 0600 sur l'hote. Il
# porte la chaine `userid` — qui, en mode wallet, ne contient aucun
# secret. Le parfile n'est jamais journalise en clair, et il est supprime
# a la sortie : c'est la seule facon d'eviter qu'un secret y survive.
#
# Le controle du deroulement repose sur trois signaux, tous
# independants de la langue des messages :
#   1. le code de sortie du client Data Pump ;
#   2. l'absence de code ORA-xxxxx/UDI-xxxxx dans le journal ;
#   3. l'etat du job dans DBA_DATAPUMP_JOBS, pour distinguer un job
#      reellement termine d'un client detache qui a laisse tourner le
#      job en arriere-plan.

osd_tool=${1:-}
osd_parfile=${2:-}
osd_timeout=${3:-0}

# `${N:-}` plutot que `$N` : l'absence d'argument doit se voir par un
# controle explicite, qui emet un `OSD_FATAL` nommant l'argument et un
# code 64 — « invocation incorrecte », distinct d'une erreur interne.
# Une lecture nue de `$N` laisserait passer une chaine vide et ferait
# echouer l'outil sans dire a l'exploitant ce qu'il doit corriger. La
# forme `${N:-}` reste la bonne quelle que soit la politique du prelude
# sur `set -u` : c'est elle qui fournit la chaine vide sur laquelle porte
# ce controle.
if [ -z "$osd_tool" ] || [ -z "$osd_parfile" ]; then
    osd_die "remote_datapump: arguments incomplets" 64
fi

#: Nombre de lignes de sortie recopiees sur stderr en fin d'execution.
#: Borne, et non « tout » : un journal d'export atteint couramment
#: plusieurs mega-octets, et noyer le journal du serveur de saut sous la
#: sortie d'un `expdp` reussis repousserait l'information des etapes
#: suivantes, qui sont celles dont on a besoin ensuite.
osd_tail_lines=60

if [ ! -f "$osd_parfile" ]; then
    osd_die "parfile absent sur l'hote: $osd_parfile" 66
fi

# Le parfile porte `userid` — un mot de passe en mode repli — et doit
# disparaitre meme si `expdp` echoue, si le script est interrompu, ou si
# la session SSH se coupe. Le `trap` du prelude est donc enregistre
# **avant** tout controle susceptible de sortir, et pas seulement
# avant la lecture : place apres la liste blanche, il laissait le
# fichier en place sur le `osd_die` de cette liste — c'est-a-dire
# exactement le cas d'un nom d'outil errone, ou l'operateur ne pense
# pas au secret.
osd_register_cleanup "$osd_parfile"

case "$osd_tool" in
    expdp|impdp) ;;
    *) osd_die "remote_datapump: outil inconnu ($osd_tool)" 64 ;;
esac

if ! osd_have "$osd_tool"; then
    osd_kv OSD_MISSING_CMD "$osd_tool"
    osd_die "$osd_tool absent du PATH sur cet hote" 127
fi

# Valeur d'une option du parfile, guillemets retires.
#
# Le nom de l'option est fourni sous forme de **classes de caracteres**
# plutot que sous sa forme litterale : Data Pump accepte `LOGFILE=` et
# `logfile=` indifféremment, et le parfile produit par le Python est en
# minuscules. `sed` n'a pas d'equivalent POSIX de `\i`, donc
# l'insensibilite seecrit `[Ll][Oo][Gg]...`. C'est plus long a ecrire,
# et c'est la seule facon de l'exprimer en POSIX.
#
# `sed -n` ... `p` est employee plutot que `grep` : le motif peut
# commencer par un caractere de classe, et l'absence de toute ligne
# correspondante doit produire une chaine vide, pas un code d'erreur qui
# remonterait dans `$?`.
osd_unque() { sed 's/^"//; s/"$//'; }
osd_pvalue() {
    sed -n "s/^$1[ \\t]*=[ \\t]*//p" "$osd_parfile" 2>/dev/null \
        | head -1 | osd_unque
}

osd_opt_logfile='[Ll][Oo][Gg][Ff][Ii][Ll][Ee]'
osd_opt_dumpfile='[Dd][Uu][Mm][Pp][Ff][Ii][Ll][Ee]'
osd_opt_directory='[Dd][Ii][Rr][Ee][Cc][Tt][Oo][Rr][Yy]'

osd_log=$(osd_pvalue "$osd_opt_logfile")
osd_dump=$(osd_pvalue "$osd_opt_dumpfile")
osd_dir=$(osd_pvalue "$osd_opt_directory")
[ -n "$osd_log" ]  && osd_kv OSD_LOGFILE "$osd_log"
[ -n "$osd_dump" ] && osd_kv OSD_DUMPFILE "$osd_dump"
[ -n "$osd_dir" ]  && osd_kv OSD_DIRECTORY "$osd_dir"

# Journal Data Pump : sa **taille**, et non son contenu. Un journal de
# plusieurs mega-octets ne transite pas par le canal machine ; seule sa
# taille indique si le job a produit autre chose que sa trace d'erreur.
#
# Le chemin se deduit du couple `directory` / `logfile` du parfile, et
# **les deux** ont leurs guillemets retires. Le code precedent
# comparait `[ -f "$osd_journal" ]` a la valeur *brute* du `logfile`,
# `"exp.log"` guillemets compris : le test etait donc toujours faux, et
# `OSD_JOURNAL_BYTES` restait absent meme sur un export ayant reussi.
# Aucun consommateur ne le rendait indispensable, mais un diagnostic
# qui manque silencieusement est un diagnostic qu'on ne peut pas
# distinguer d'une absence de probleme.
if [ -n "$osd_log" ] && [ -n "$osd_dir" ]; then
    osd_journal=$osd_dir/$osd_log
    if [ -f "$osd_journal" ]; then
        osd_kv OSD_JOURNAL_BYTES "$(wc -c <"$osd_journal" | tr -d ' ')"
    fi
fi

# `timeout` n'est pas POSIX et peut manquer sur AIX. On ne l'utilise que
# s'il est present ; sinon le controle de duree est laisse au serveur de
# saut, qui peut toujours interrompre la session SSH.
osd_use_timeout=0
if [ "$osd_timeout" -gt 0 ] 2>/dev/null && osd_have timeout; then
    osd_use_timeout=1
fi

osd_stdout=$(osd_tmpfile dpout) || osd_die "fichier temporaire impossible" 70

# stdin est ferme sur `/dev/null`, et c'est **indispensable**.
#
# Le client Data Pump accuse reception d'un `userid` de la forme
# « / as sysdba » — une connexion authentifiee par le systeme
# d'exploitation, seule forme acceptee sur ces hotes — en affichant
# « Password: ». La connexion aboutit (OS), mais le client consomme
# quand meme une ligne sur stdin.
#
# Herite via Ansible, stdin n'est ni un terminal ni un fichier clos : la
# lecture ne rend jamais la main, et l'export ne se termine jamais. C'est
# un blocage, pas une lenteur : mesure sur l'hote, le meme export sans
# redirection de stdin n'a pas rendu la main en 240 s, et en 66 s avec
# elle, pour un schema de 2 Mo.
#
# `< /dev/null` rend la lecture instantanee (fin de fichier). Le client
# garde le `userid` du parfile et se connecte par le systeme
# d'exploitation : aucune saisie n'est reellement attendue.
if [ "$osd_use_timeout" -eq 1 ]; then
    timeout "$osd_timeout" "$osd_tool" parfile="$osd_parfile" \
        < "/dev/null" >"$osd_stdout" 2>&1
    osd_rc=$?
else
    "$osd_tool" parfile="$osd_parfile" \
        < "/dev/null" >"$osd_stdout" 2>&1
    osd_rc=$?
fi

osd_kv OSD_RC "$osd_rc"
osd_kv OSD_TIMEOUT_ENFORCED "$osd_use_timeout"

# Erreurs Oracle / Data Pump, independantes de la langue.
if grep -qE '(ORA|UDI|DBMGSPC)-[0-9]+' "$osd_stdout" 2>/dev/null; then
    osd_kv OSD_DATAPUMP_ERROR 1
    # Extraction POSIX : `grep -oE` n'existe pas sur AIX, et le
    # `|| true` qui suivrait masquerait la perte au lieu de la
    # signaler. `osd_codes` fait le meme travail en POSIX strict.
    ( cat "$osd_stdout" 2>/dev/null ) \
        | osd_codes '(ORA|UDI|DBMGSPC)-[0-9]+' \
        > "$osd_stdout.codes" 2>/dev/null || true
    if [ -s "$osd_stdout.codes" ]; then
        osd_kv OSD_ERROR_CODES "$(cat "$osd_stdout.codes")"
    fi
else
    osd_kv OSD_DATAPUMP_ERROR 0
fi

# La sortie du client ne doit **jamais** disparaitre. Le code la
# capturait dans `$osd_stdout` pour en extraire les codes, puis
# supprimait le fichier : en cas d'echec, l'exploitant recevait
# `ORA-39002` sans une seule ligne de contexte, alors que la cause se
# trouvait dans ces quelques kilo-octets — un nom de tablespace
# inexistant, un privilege manquant, un fichier de dump absent.
#
# `ORA-39002: invalid operation` ne designe rien d'actionnable. C'est
# la ligne suivante qui dit quoi faire.
#
# Le traitement est deliberement borne a la **fin** de la sortie :
#   * `tail` est POSIX, `tail -c` ne l'est pas sur toutes les versions
#     d'AIX, et un journal d'export atteint couramment plusieurs
#     mega-octets ;
#   * c'est la fin qui contient le diagnostic : Data Pump ecrit le
#     recapitulatif, puis l'erreur, puis s'arrete ;
#   * un volume borne evite qu'un export reussi noie le journal du
#     serveur de saut et repousse l'information des autres etapes.
#
# Le tout va sur **stderr**, donc dans les journaux du serveur de saut
# et jamais dans le bloc machine : stdout ne porte que l'inventaire
# d'objets, ou une ligne de diagnostic serait comptee comme un objet.
if [ -s "$osd_stdout" ]; then
    tail -n "$osd_tail_lines" "$osd_stdout" 2>/dev/null \
        | sed 's/^/  [datapump] /' >&2 2>/dev/null || true
fi

# `osd_stdout` est deja declare par `osd_tmpfile` ; le fichier de codes
# ne l'est pas, il etant produit par une redirection dans le corps.
rm -f "$osd_stdout.codes" 2>/dev/null || true
osd_exit "$osd_rc"

# OSD remote_sqlplus — execution d'un script SQL sur un hote Oracle.
#
# Le SQL est fourni en argument par le Python, entre simples quotes dans
# une variable du script distant. Il n'apparait donc :
#   * ni dans la ligne de commande du processus distant (qui n'est que
#     `sh -s`), donc pas dans `ps`;
#   * ni dans les journaux du serveur de saut, ou il est masque par le
#     filtre de redaction au cas ou il contiendrait un secret.
#
# Le SQL ecrit dans un fichier temporaire en 0600, supprime systematiquement.
#
# Usage :
#   remote_sqlplus <connect> <sql>
#     <connect>  EZCONNECT //h:1521/SVC, alias TNS, ou /@alias (wallet)
#     <sql>      instructions SQL, sans `exit` (ajoute ici)
#
# Le corps ne depend PAS de la NLS_LANG du client : les erreurs sont
# detectees par `whenever sqlerror exit failure` et par recherche de
# `ORA-xxxxx` dans la sortie brute, jamais par un texte traduit.

# `${N:-}` et non `$N` : l'absence d'argument doit se voir par un controle
# explicite, qui produit un code 64 (« invocation incorrecte ») et un
# `OSD_FATAL` nommant l'argument. Une lecture nue de `$N` donnerait un
# code 1, indistinct d'une erreur interne. La forme `${N:-}` reste la bonne
# quelle que soit la politique du prelude sur `set -u` : c'est elle qui
# fournit la chaine vide sur laquelle porte ce controle.
osd_connect=${1:-}
osd_sql=${2:-}

if [ -z "$osd_connect" ]; then
    osd_die "remote_sqlplus: chaine de connexion absente" 64
fi
if [ -z "$osd_sql" ]; then
    osd_die "remote_sqlplus: requete absente" 64
fi
if ! osd_have sqlplus; then
    osd_kv OSD_MISSING_CMD sqlplus
    osd_die "sqlplus absent du PATH sur cet hote" 127
fi

osd_sqlfile=$(osd_tmpfile sql) || osd_die "fichier temporaire impossible" 70

# Sortie deterministe :
#   - heading/feedback off : pas de mise en forme, une valeur par ligne ;
#   - pagesize 0           : pas de pause sur un terminal absent ;
#   - trimspool on         : pas de blancs de fin de ligne ;
#   - colsep               : separateur de colonnes choisi par le Python
#                            (`~|`), sur un alphabet absent des
#                            identifiants valides, donc non ambigu.
# `whenever sqlerror exit failure` transforme toute erreur Oracle en code
# de sortie non nul, independamment de sa formulation.
{
    printf 'set heading off feedback off pagesize 0 linesize 32767 trimspool on echo off verify off feedback off\n'
    # `colsep` est indispensable : sans lui, SQL*Plus aligne les colonnes
    # avec des blancs et des tabulations, et un `select a, b from ...`
    # devient impossible a decouper de facon fiable. Le separateur est
    # choisi par le Python (`~|`) sur un alphabet absent des
    # identifiants valides : `colsplit` n'est donc pas dans le paquet
    # d'expressions d'un nom d'objet.
    printf 'set colsep "%s"\n' '~|'
    # `define` indisponible ici : la substitution `&` serait
    # desactivee, ce qui evite qu'un `&` dans une donnee soit pris pour
    # une variable de substitution.
    printf 'set define off\n'
    # `sqlblanklines` evite qu'un `set` soit rejete a cause de sa mise en
    # forme ; `sqlblanklines` est plus connu sous le nom `sqlblanklines`
    # sur les versions recentes, absent sur 19c de certaines versions.
    # On s'en abstient : le SQL produit par l'outil n'a pas de ligne vide.
    printf 'whenever sqlerror exit failure\n'
    printf '%s\n' "$osd_sql"
    # **Terminateur obligatoire.** SQL*Plus met les instructions en
    # tampon jusqu'a un `;` ou un `/` : un script dont la derniere
    # instruction n'est pas terminsee n'est jamais execute, et le
    # `exit` suivant le jette. Symptome trompeur : code de sortie 0,
    # aucune sortie, aucun message — un « succes » parfaitement vide.
    #
    # On complete donc le SQL plutot que d'exiger du Python qu'il
    # terminise chaque chaine : l'exigence serait invisible a la lecture
    # d'un appel de `query()`, et son oubli passerait inapercu jusqu'a
    # une decision prise sur un resultat vide.
    #
    # Le cas `end;` d'un bloc PL/SQL est deja termine ; le cas `/` est
    # accepte tel quel. Une instruction terminee par un point-virgule
    # suivi de commentaires reste traitee comme terminee.
    case "$osd_sql" in
        *\;|*\;[[:space:]]*|/[[:space:]]*|*/) : ;;
        *) printf ';\n' ;;
    esac
    printf 'exit\n'
} > "$osd_sqlfile"

osd_out=$(osd_tmpfile out) || osd_die "fichier temporaire impossible" 70
osd_err=$(osd_tmpfile err) || osd_die "fichier temporaire impossible" 70

# stdin est ferme sur `/dev/null`, comme dans `remote_datapump.sh`.
#
# SQL*Plus peut demander une saisie : un mot de passe absent d'une chaine
# de connexion, ou la confirmation « Appuyez sur Entree » en fin de script
# si une commande laisse le curseur en attente. Le script passe par
# `@fichier` et n'a donc **rien** a lire sur stdin. Herite via Ansible, ce
# descripteur n'est ni un terminal ni un fichier clos : la lecture ne rend
# jamais la main, et l'etape bloque indefiniment — ce qui est arrive sur
# l'etape 4, avant que `set -u` ne soit rendu visible.
sqlplus -S -L "$osd_connect" @"$osd_sqlfile" \
    < "/dev/null" >"$osd_out" 2>"$osd_err"
osd_rc=$?

osd_kv OSD_RC "$osd_rc"
osd_kv OSD_OUT_BYTES "$(wc -c <"$osd_out" | tr -d ' ')"

# Detection d'erreur Oracle insensible a la locale : ORA-xxxxx.
# Cette verification estIndependante du code de sortie, qui peut etre 0
# si l'erreur est survenue dans une section non fatale du script.
if grep -q 'ORA-[0-9][0-9]*' "$osd_out" "$osd_err" 2>/dev/null; then
    osd_kv OSD_ORACLE_ERROR 1
    # Codes d'erreur rencontres, dedoublonnes. `cat` avant `grep` evite
    # le prefixe de nom de fichier que grep ajoute des lors que la liste
    # en compte plusieurs, et evite l'option -h qui n'est pas garantie
    # sur AIX.
    # Extraction POSIX : ni `grep -o`, ni `-h` (absent ou divergent sur
    # AIX). Le motif est passe a `osd_codes` qui fait le travail.
    ( cat "$osd_out" "$osd_err" 2>/dev/null ) \
        | osd_codes 'ORA-[0-9][0-9]*' > "$osd_out.codes" 2>/dev/null || true
    if [ -s "$osd_out.codes" ]; then
        osd_kv OSD_ORACLE_CODES "$(cat "$osd_out.codes")"
    fi
else
    osd_kv OSD_ORACLE_ERROR 0
fi

if [ -s "$osd_out" ]; then
    osd_rows_begin
    # `osd_rows_file`, et non `cat` : voir le commentaire du prelude.
    # Une requete dont le resultat ne se termine pas par un saut de
    # ligne — `select ... from dual` sans `;` final sur certaines
    # versions — collerait sa derniere valeur au marqueur de fin.
    osd_rows_file "$osd_out"
    osd_rows_end
fi

if [ -s "$osd_err" ]; then
    sed 's/^/  [sqlplus] /' "$osd_err" >&2 2>/dev/null || true
fi

rm -f "$osd_sqlfile" "$osd_out" "$osd_err" "$osd_out.codes" 2>/dev/null || true
osd_exit "$osd_rc"

#!/usr/bin/env bash
# Fixture de test d'integration : cree un schema source et un schema
# cible sur l'instance locale, avec des objets representatifs.
#
# Ce script PREPARE l'environnement de test. Il n'est execute que par les
# tests d'integration, qui supposent un acces SYSDBA local.
#
# Il cree :
#   * OSD_SRC : tables, index, contrainte unique et cle etrangere,
#                sequence, vue, trigger, grant, synonym, un objet
#                volontairement invalide, et 20 000 lignes pour que
#                l'estimation d'espace de l'etape 9 soit realiste ;
#   * OSD_TGT : schema vide et des droits identiques, cible de la
#                duplication.
#
# Usage :
#   OSD_TEST_PW=<mot de passe> tests/integration/setup_fixture.sh [create|drop]
#
# Le mot de passe vient de l'environnement et n'est ecrit dans aucun
# fichier du depot. Le SQL est produit dans un fichier temporaire en 0600,
# supprime a la sortie.

set -u

export ORACLE_SID="${ORACLE_SID:-OEMCC}"
TEST_PW="${OSD_TEST_PW:-}"
ACTION="${1:-create}"
SRC="${OSD_TEST_SRC:-OSD_SRC}"
TGT="${OSD_TEST_TGT:-OSD_TGT}"

if [ -z "$TEST_PW" ]; then
    echo "OSD_TEST_PW non defini" >&2
    echo "Ce fixture cree un compte applicatif : un mot de passe est" >&2
    echo "necessaire pour exercer le chemin de repli de l'outil." >&2
    exit 64
fi

SQLF=$(mktemp "${TMPDIR:-/tmp}/osd-fixture.XXXXXX.sql") || exit 70
trap 'rm -f "$SQLF"' EXIT INT TERM
chmod 600 "$SQLF"

# `set define off` desactive la substitution `&`, qui sinon tenterait
# d'interpreter `&` dans les predicats et les privileges.
emit_header() {
    cat >> "$SQLF" <<'EOSQL'
set heading off feedback off pagesize 0 linesize 32767 trimspool on echo off verify off
set define off
whenever sqlerror continue
exit
EOSQL
}

: > "$SQLF"

if [ "$ACTION" = "drop" ]; then
    {
        printf 'set heading off feedback off pagesize 0 linesize 32767 echo off\n'
        printf 'set define off\nwhenever sqlerror continue\n'
        printf 'drop user %s cascade;\n' "$SRC"
        printf 'drop user %s cascade;\n' "$TGT"
        printf 'exit\n'
    } > "$SQLF"
    sqlplus -S / as sysdba @"$SQLF" >/dev/null 2>&1
    echo "fixture supprimee ($SRC, $TGT)"
    exit 0
fi

# ---- Phase 1 : comptes et privileges ----------------------------------
{
    printf 'set heading off feedback off pagesize 0 linesize 32767 echo off\n'
    printf 'set define off\nwhenever sqlerror exit failure\n'
    printf "create user %s identified by '%s' default tablespace users quota unlimited on users;\n" "$SRC" "$TEST_PW"
    printf "create user %s identified by '%s' default tablespace users quota unlimited on users;\n" "$TGT" "$TEST_PW"
    printf "grant create session, create table, create sequence, create view, create trigger, create procedure to %s;\n" "$SRC"
    printf "grant create session, create table, create sequence, create view, create trigger, create procedure to %s;\n" "$TGT"
    printf "grant read, write on directory DATA_PUMP_DIR to %s;\n" "$SRC"
    printf "grant read, write on directory DATA_PUMP_DIR to %s;\n" "$TGT"
    printf "grant exp_full_database to %s;\n" "$SRC"
    printf "grant imp_full_database to %s;\n" "$TGT"
    printf "grant select_catalog_role to %s, %s;\n" "$SRC" "$TGT"
    printf 'exit\n'
} > "$SQLF"

if ! sqlplus -S / as sysdba @"$SQLF" 2>&1 | grep -qv '^$'; then
    :  # sortie vide = succes
fi
if ! sqlplus -S / as sysdba @"$SQLF" >/dev/null 2>&1; then
    echo "Echec de la creation des comptes :" >&2
    sqlplus -S / as sysdba @"$SQLF" 2>&1 | grep -i 'ORA-' >&2 || true
    exit 1
fi

# ---- Phase 2 : objets metier -----------------------------------------
# Les instructions sont separees par des lignes vides, ce qui est
# delimiteur d'instruction pour SQL*Plus : chaque CREATE est donc traite
# isolement, et `whenever sqlerror continue` permet de voir toutes les
# erreurs d'un coup plutot que de s'arreter a la premiere.
{
    printf 'set heading off feedback off pagesize 0 linesize 32767 echo off\n'
    printf 'set define off\nwhenever sqlerror continue\n'
    printf "create table %s.PERSONNE (\n" "$SRC"
    printf '  id       number(10) not null,\n'
    printf '  nom      varchar2(50) not null,\n'
    printf '  prenom   varchar2(50),\n'
    printf '  email    varchar2(120),\n'
    printf '  cree_le  date default sysdate,\n'
    printf '  constraint pk_personne primary key (id),\n'
    printf '  constraint uq_personne_email unique (email)\n'
    printf ');\n\n'
    printf "create index ix_personne_nom on %s.PERSONNE (nom, prenom);\n\n" "$SRC"
    printf "create table %s.ADRESSE (\n" "$SRC"
    printf '  id          number(10) not null,\n'
    printf '  personne_id number(10) not null,\n'
    printf '  ville       varchar2(80)\n'
    printf ');\n\n'
    printf "alter table %s.ADRESSE add constraint fk_adresse_pers\n" "$SRC"
    printf '  foreign key (personne_id) references %s.PERSONNE (id);\n\n' "$SRC"
    printf "create table %s.LOG_EVT (\n" "$SRC"
    printf '  id      number(10) not null,\n'
    printf '  quand   timestamp default systimestamp,\n'
    printf '  niveau  varchar2(10),\n'
    printf '  message varchar2(4000)\n'
    printf ');\n\n'
    printf "create index ix_log_quand on %s.LOG_EVT (quand);\n\n" "$SRC"
    printf "create sequence %s.SQ_PERSONNE start with 1000 increment by 1;\n\n" "$SRC"
    printf "create view %s.V_PERSONNES as select id, nom, prenom from %s.PERSONNE;\n\n" "$SRC" "$SRC"
    printf "create table %s.OBJET_CASSE (\n" "$SRC"
    printf '  id number primary key,\n'
    printf "  vue_col reference %s.vue_bidon(col)\n" "$SRC"
    printf ');\n\n'
    printf "insert into %s.PERSONNE (id, nom, prenom) values (1, 'Dupont', 'Jean');\n" "$SRC"
    printf "insert into %s.PERSONNE (id, nom, prenom) values (2, 'Martin', 'Claire');\n" "$SRC"
    printf "insert into %s.ADRESSE values (1, 1, 'Paris');\n" "$SRC"
    printf "insert into %s.ADRESSE values (2, 2, 'Lyon');\n" "$SRC"
    printf "insert into %s.LOG_EVT (niveau, message)\n" "$SRC"
    printf "  select 'INFO', 'evenement de test ' || level from dual connect by level <= 20000;\n"
    printf 'commit;\n\n'
    printf "create or replace trigger %s.trg_personne_biu\n" "$SRC"
    printf "before insert or update on %s.PERSONNE for each row\n" "$SRC"
    printf 'begin\n'
    printf '  if :new.email is null then\n'
    printf "    :new.email := lower(:new.prenom) || chr(64) || lower(:new.nom) || chr(46) || lower(:new.prenom);\n"
    printf '  end if;\n'
    printf 'end;\n/\n\n'
    printf 'exit\n'
} > "$SQLF"

sqlplus -S / as sysdba @"$SQLF" >/dev/null 2>&1

# ---- Phase 3 : dependances croisees (grant + synonym) ----------------
{
    printf 'set heading off feedback off pagesize 0 linesize 32767 echo off\n'
    printf 'set define off\nwhenever sqlerror continue\n'
    printf "grant select on %s.PERSONNE to %s;\n" "$SRC" "$TGT"
    printf "create synonym %s.syn_adr for %s.ADRESSE;\n" "$TGT" "$SRC"
    printf 'exit\n'
} > "$SQLF"
sqlplus -S / as sysdba @"$SQLF" >/dev/null 2>&1

# ---- Bilan -----------------------------------------------------------
{
    printf 'set heading off feedback off pagesize 0 linesize 32767\n'
    printf 'set define off\n'
    printf "select 'objets_src=' || count(*) from dba_objects where owner = '%s';\n" "$SRC"
    printf "select 'octets_src=' || nvl(sum(bytes),0) from dba_segments where owner = '%s';\n" "$SRC"
    printf "select 'lignes_log=' || count(*) from %s.LOG_EVT;\n" "$SRC"
    printf "select 'invalides_src=' || count(*) from dba_objects where owner = '%s' and status = 'INVALID';\n" "$SRC"
    printf "select 'objets_tgt=' || count(*) from dba_objects where owner = '%s';\n" "$TGT"
    printf 'exit\n'
} > "$SQLF"

echo "fixture prete : $SRC -> $TGT"
sqlplus -S / as sysdba @"$SQLF" 2>/dev/null | grep -E '^(objets_src|octets_src|lignes_log|invalides_src|objets_tgt)=' | sed 's/^/  /'

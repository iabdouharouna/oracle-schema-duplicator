"""Sonde : quels GRANT le compte source a-t-il recus / donnes ?

Read-only. On regarde `dba_tab_privs` des deux cotes :
  * grants RECUS par le schema source (grantee = source) : souvent des
    grants SYS sur des vues systeme ; un export Data Pump de schema ne
    les emporte PAS.
  * grants DONNES par le schema source a d'autres (owner = source,
    grantee <> source) : l'import les rejoue et echoue en ORA-01917 si
    le grantee n'existe pas sur la cible.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
src = str(cfg.get("SOURCE_SCHEMA"))
tgt = str(cfg.get("TARGET_SCHEMA"))
pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
sa = pipe.source_adapter
ta = pipe.target_adapter

print("== admin_option des privileges SYSTEME source vs cible ==")
for label, ad, schema in (("source", sa, src), ("cible", ta, tgt)):
    rows = ad.query(
        "select admin_option, count(*) from dba_sys_privs "
        f"where grantee = '{schema}' group by admin_option")
    print(f"   {label}: {rows}")

print("\n== Grants RECUS par le schema source (grantee = %s) ==" % src)
rows = sa.query(
    "select owner, count(*) from dba_tab_privs "
    f"where grantee = '{src}' group by owner order by 2 desc")
tot = 0
for r in rows:
    print("   ", r)
    tot += int(r[1])
print("   total lignes :", tot)

print("\n== Exemples de grants recus depuis SYS (15) ==")
for r in sa.query(
    "select table_name, privilege, grantable from dba_tab_privs "
    f"where grantee = '{src}' and owner = 'SYS' "
    "order by table_name fetch first 15 rows only"):
    print("   ", r)

print("\n== Grants DONNES par le schema source a d'autres ==")
rows = sa.query(
    "select grantee, count(*) from dba_tab_privs "
    f"where owner = '{src}' and grantee <> '{src}' "
    "group by grantee order by 2 desc")
for r in rows:
    print("   ", r)

print("\n== Ces grantees existent-ils sur la CIBLE ? ==")
for r in rows:
    g = r[0]
    ex_u = ta.query(f"select 1 from dba_users where username = '{g}'")
    ex_r = ta.query(f"select 1 from dba_roles where role = '{g}'")
    print(f"    {g:28s} user={'oui' if ex_u else 'NON':3s} "
          f"role={'oui' if ex_r else 'NON'}")

print("\n== Grants d'objet que la CIBLE a recus (grantee = %s) ==" % tgt)
rows2 = ta.query(
    "select owner, count(*) from dba_tab_privs "
    f"where grantee = '{tgt}' group by owner order by 2 desc")
for r in rows2:
    print("   ", r)
if not rows2:
    print("    (aucun)")

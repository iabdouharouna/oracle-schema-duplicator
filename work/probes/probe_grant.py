"""Sonde : la creation du schema cible reconduit-elle les GRANT systeme ?

Read-only. On lit les privileges systeme, roles et quotas du compte source,
et on regarde ce que `plan_create_target_schema` en fait. Le `create user`
porte l'empreinte du mot de passe : on ne l'imprime pas, seuls les GRANT.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.schema import plan_create_target_schema
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
src = str(cfg.get("SOURCE_SCHEMA"))
tgt = str(cfg.get("TARGET_SCHEMA"))
print(f"source={src}  cible={tgt}  CREATE_TARGET_SCHEMA="
      f"{cfg.get('CREATE_TARGET_SCHEMA')}")

pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
sa = pipe.source_adapter
ta = pipe.target_adapter

print("\n== DBA_SYS_PRIVS (directs) sur la source ==")
rows = sa.query(f"select privilege, admin_option from dba_sys_privs "
                f"where grantee = '{src}'")
for r in rows:
    print("  ", r)
print(f"  total : {len(rows)}")

print("\n== DBA_ROLE_PRIVS sur la source ==")
rows = sa.query(f"select granted_role, admin_option, default_role "
                f"from dba_role_privs where grantee = '{src}'")
for r in rows:
    print("  ", r)
print(f"  total : {len(rows)}")

print("\n== DBA_TS_QUOTAS sur la source ==")
for r in sa.query(f"select tablespace_name, max_bytes from dba_ts_quotas "
                  f"where username = '{src}'"):
    print("  ", r)

print("\n== Plan genere par plan_create_target_schema ==")
plan = plan_create_target_schema(
    sa, ta, source_schema=src, target_schema=tgt,
    remap_tablespace=cfg.get("REMAP_TABLESPACE"),
)
for st in plan.statements:
    if st.lower().startswith("grant"):
        print("  ", st)
    else:
        # Ne jamais imprimer le create user (empreinte du mot de passe).
        print("   [create user ...] (masque)")
print("  --- detail ---")
for d in plan.detail:
    print("  ", d)
print("  --- warnings ---")
for w in plan.warnings:
    print("  ", w)

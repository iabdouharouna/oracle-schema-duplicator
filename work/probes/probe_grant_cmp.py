"""Sonde : comparer les GRANT systeme entre le compte source et le compte cible.

Read-only.
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


def dump(ad, label, schema):
    print(f"\n== {label} : {schema} ==")
    print("  utilisateur :")
    for r in ad.query(
        "select username, account_status, default_tablespace, "
        "temporary_tablespace, profile from dba_users "
        f"where username = '{schema}'"):
        print("   ", r)
    privs = ad.query(
        f"select privilege from dba_sys_privs where grantee = '{schema}'")
    print(f"  privileges systeme ({len(privs)}) :")
    for r in privs:
        print("     ", r[0])
    roles = ad.query(
        f"select granted_role from dba_role_privs where grantee = '{schema}'")
    print(f"  roles ({len(roles)}) :")
    for r in roles:
        print("     ", r[0])


dump(sa, "SOURCE", src)
dump(ta, "CIBLE", tgt)

# Que manque-t-il a la cible ?
ps = {r[0].strip() for r in sa.query(
    f"select privilege from dba_sys_privs where grantee = '{src}'")}
pt = {r[0].strip() for r in ta.query(
    f"select privilege from dba_sys_privs where grantee = '{tgt}'")}
print("\n== Privileges systeme MANQUANTS sur la cible ==")
for p in sorted(ps - pt):
    print("   -", p)
print(f"   ({len(ps - pt)} manquant(s) sur {len(ps)})")

rs = {r[0].strip() for r in sa.query(
    f"select granted_role from dba_role_privs where grantee = '{src}'")}
rt = {r[0].strip() for r in ta.query(
    f"select granted_role from dba_role_privs where grantee = '{tgt}'")}
print("\n== Roles MANQUANTS sur la cible ==")
for r in sorted(rs - rt):
    print("   -", r)

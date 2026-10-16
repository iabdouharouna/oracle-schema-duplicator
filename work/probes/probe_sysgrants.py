"""Sonde : detail des 22 grants d'objet SYS recus par la source, et impact.

Read-only. Pour chaque grant recu par la source (owner SYS), on regarde si
le meme privilege est deja ouvert a PUBLIC sur la cible (donc deja
utilisable par POWERCARDBO1), ou s'il manque reellement.
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

recus = sa.query(
    "select owner, table_name, privilege, grantable from dba_tab_privs "
    f"where grantee = '{src}' order by owner, table_name, privilege")
print(f"== {len(recus)} grants d'objet recus par {src} ==")

# Privileges ouverts a PUBLIC sur la cible, par (owner, table, privilege)
pub = {(r[0], r[1], r[2]) for r in ta.query(
    "select owner, table_name, privilege from dba_tab_privs "
    "where grantee = 'PUBLIC'")}

manquant = 0
for owner, table, priv, grantable in recus:
    cover = "PUBLIC ok" if (owner, table, priv) in pub else "MANQUE"
    if cover == "MANQUE":
        manquant += 1
    print(f"   {owner}.{table:<28s} {priv:<9s} grantable={grantable}  {cover}")

print(f"\n   -> {manquant} grant(s) explicitement presents sur la source "
      f"mais ni sur {tgt} ni via PUBLIC")

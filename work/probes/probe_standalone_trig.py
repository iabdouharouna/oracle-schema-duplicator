"""Sonde : distinguer un trigger propre a la table (sa base) d'un trigger
d'une AUTRE table dont le corps reference la table.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import _standalone_tables, _table_names
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
ad = pipe.source_adapter

isolees = set(_standalone_tables(ad, schema))
tables = set(_table_names(ad, schema))

# base de chaque trigger
base = {}
for r in ad.query(
    "select trigger_name, table_name from dba_triggers "
    f"where owner = '{schema}'"
):
    if len(r) > 1:
        base[str(r[0]).strip()] = str(r[1]).strip()

# aretes trigger -> table
propres, corps = set(), set()
for r in ad.query(
    "select name, referenced_name from dba_dependencies "
    f"where owner = '{schema}' and referenced_owner = '{schema}' "
    "and referenced_type = 'TABLE' and type = 'TRIGGER'"
):
    if len(r) < 2:
        continue
    trig, ref = str(r[0]).strip(), str(r[1]).strip()
    if base.get(trig) == ref:
        propres.add(ref)
    else:
        corps.add(ref)

print(f"isolees={len(isolees)}")
print(f"touchees par trigger sur elle-meme (sa base) = {len(isolees & propres)}")
print(f"touchees par trigger d'une AUTRE table (corps) = {len(isolees & corps)}")

# ensemble disqualifiant "code + trigger d'une autre table"
CODE = {"VIEW", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION",
        "PACKAGE", "PACKAGE BODY", "TYPE", "TYPE BODY"}
util = set(corps)
for r in ad.query(
    "select referenced_name from dba_dependencies "
    f"where owner = '{schema}' and referenced_owner = '{schema}' "
    "and referenced_type = 'TABLE' and type in "
    "('VIEW','MATERIALIZED VIEW','PROCEDURE','FUNCTION','PACKAGE',"
    "'PACKAGE BODY','TYPE','TYPE BODY')"
):
    if r:
        util.add(str(r[0]).strip())

print(f"\nisolees restantes (code + trigger-autre-table) = {len(isolees - util)}")
print(f"  dont disqualifiees par code seul = {len(isolees & util & (util - corps))}")

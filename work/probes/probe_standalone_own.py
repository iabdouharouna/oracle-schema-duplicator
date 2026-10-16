"""Sonde : usage des tables en ignorant les triggers propres a la table.

Deux lectures : dba_dependencies (referenced_name, name, type) et
dba_triggers (trigger_name, table_name) pour savoir sur quelle table un
trigger est pose.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
ad = pipe.source_adapter

FK = (
    "select t.table_name from dba_tables t "
    f"where t.owner = '{schema}' "
    "and not exists (select 1 from dba_constraints c "
    "where c.owner = t.owner and c.table_name = t.table_name "
    "and c.constraint_type = 'R') "
    "and not exists (select 1 from dba_constraints p "
    "join dba_constraints c "
    "on c.r_owner = p.owner "
    "and c.r_constraint_name = p.constraint_name "
    "where p.owner = t.owner and p.table_name = t.table_name "
    "and c.constraint_type = 'R') order by t.table_name"
)

t0 = time.time()
fk = {str(r[0]).strip() for r in ad.query(FK) if r}
print(f"FK seule : {time.time()-t0:.1f}s -> {len(fk)}")

t0 = time.time()
deps = ad.query(
    "select referenced_name, name, type from dba_dependencies "
    f"where referenced_owner = '{schema}' and referenced_type = 'TABLE'"
)
print(f"dba_dependencies : {time.time()-t0:.1f}s -> {len(deps)} lignes")

t0 = time.time()
trig = ad.query(
    "select trigger_name, table_name from dba_triggers "
    f"where owner = '{schema}'"
)
print(f"dba_triggers : {time.time()-t0:.1f}s -> {len(trig)} lignes")

base = {str(r[0]).strip(): str(r[1]).strip() for r in trig if len(r) > 1}
util = set()
for r in deps:
    if len(r) < 3:
        continue
    ref, nom, typ = (str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip())
    if typ == "TABLE":
        continue
    if typ == "TRIGGER" and base.get(nom) == ref:
        continue  # trigger propre a la table : pas un usage
    util.add(ref)
print(f"\nisolees restantes (triggers propres ignores) = {len(fk - util)}")
print(f"isolées en tout (FK seule) = {len(fk)}")

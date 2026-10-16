"""Sonde : comparer requete unifiee vs FK + lecture complete + Python."""
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
    "and c.constraint_type = 'R') "
    "order by t.table_name"
)

t0 = time.time()
fk = {str(r[0]).strip() for r in ad.query(FK) if r}
t_fk = time.time() - t0

t0 = time.time()
deps = ad.query(
    "select referenced_name, type from dba_dependencies "
    f"where referenced_owner = '{schema}' and referenced_type = 'TABLE'"
)
t_dep = time.time() - t0
util = {str(r[0]).strip() for r in deps
        if len(r) > 1 and str(r[1]).strip() != "TABLE"}

print(f"FK seule                 : {t_fk:.1f}s -> {len(fk)}")
print(f"lecture dba_dependencies : {t_dep:.1f}s -> {len(deps)} lignes")
print(f"total (FK + lecture)     : {t_fk + t_dep:.1f}s")
print(f"isolees sans dependant   : {len(fk - util)}")

"""Sonde : la requete unifiee (FK + aucun dependant non-table), chronometree."""
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

# types presents dans dba_dependencies (tous referenced_type confondus)
rows = ad.query(
    "select type, count(*) from dba_dependencies "
    f"where owner = '{schema}' group by type order by 2 desc"
)
print("-- types de referenceurs (dans le schema) --")
for r in rows[:20]:
    print(f"  {str(r[0]):22s} {r[1]}")

sql = (
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
    "and not exists (select 1 from dba_dependencies d "
    "where d.referenced_owner = t.owner "
    "and d.referenced_name = t.table_name "
    "and d.referenced_type = 'TABLE' "
    "and d.type <> 'TABLE') "
    "order by t.table_name"
)
t0 = time.time()
rows = ad.query(sql)
print(f"\nrequete unifiee : {time.time()-t0:.1f}s, {len(rows)} tables isolees")

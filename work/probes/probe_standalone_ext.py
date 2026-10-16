"""Sonde : dependants de NOS tables venant d'un AUTRE schema."""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import _standalone_tables
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
ad = pipe.source_adapter

isolees = set(_standalone_tables(ad, schema))

rows = ad.query(
    "select referenced_name, owner, type from dba_dependencies "
    f"where referenced_owner = '{schema}' and owner != '{schema}' "
    "and referenced_type = 'TABLE'"
)
ext = {}
for r in rows:
    if len(r) < 3:
        continue
    ext.setdefault(str(r[0]).strip(), set()).add(str(r[1]).strip())
touche = isolees & set(ext)
print(f"isolees touchees par un dependant d'un AUTRE schema = {len(touche)}")
for t in sorted(touche)[:10]:
    print(f"  {t}: {sorted(ext[t])[:3]}")

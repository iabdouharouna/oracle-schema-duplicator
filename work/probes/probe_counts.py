"""Sonde 6 : volumetrie des vues dictionnaire par schema."""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe6"), run_id="probe6")
ad = p.source_adapter
s = str(cfg.get("SOURCE_SCHEMA"))
for label, sql in (
    ("dba_objects", f"select count(*) from dba_objects where owner='{s}'"),
    ("dba_indexes", f"select count(*) from dba_indexes where owner='{s}'"),
    ("dba_triggers", f"select count(*) from dba_triggers where owner='{s}'"),
    ("dba_lobs", f"select count(*) from dba_lobs where owner='{s}'"),
    ("dba_dependencies intra",
     f"select count(*) from dba_dependencies where owner='{s}' "
     f"and referenced_owner='{s}'"),
):
    try:
        print(f"{label:22s} = {ad.scalar(sql)}", flush=True)
    except BaseException as exc:
        print(f"{label:22s} : FAIL {type(exc).__name__} {exc}", flush=True)
print("fin", flush=True)

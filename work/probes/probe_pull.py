"""Sonde 8 : cout des lectures « dictionnaire entier » (sans liste)."""
import sys, time
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe8"), run_id="probe8")
ad = p.source_adapter
s = str(cfg.get("SOURCE_SCHEMA"))

essais = {
    "objets (inventaire)": (
        "select object_type, object_name from dba_objects "
        f"where owner = '{s}' and object_type not in "
        "('SYNONYM', 'DATABASE LINK') order by object_type, object_name"
    ),
    "carte possedes (union)": (
        "select table_name, index_name from dba_indexes "
        f"where owner = '{s}' "
        "union all select table_name, trigger_name from dba_triggers "
        f"where owner = '{s}' "
        "union all select table_name, segment_name from dba_lobs "
        f"where owner = '{s}' "
        "union all select table_name, index_name from dba_lobs "
        f"where owner = '{s}'"
    ),
    "segments (groupe)": (
        "select segment_type, segment_name, nvl(bytes, 0) from dba_segments "
        f"where owner = '{s}'"
    ),
}
for label, sql in essais.items():
    t0 = time.time()
    try:
        rows = ad.query(sql)
        dt = time.time() - t0
        print(f"{label:26s} : {dt:6.1f}s  {len(rows)} lignes", flush=True)
    except BaseException as exc:
        dt = time.time() - t0
        print(f"{label:26s} : FAIL apres {dt:.1f}s {type(exc).__name__} "
              f"{getattr(exc, 'message', exc)}", flush=True)
print("fin", flush=True)

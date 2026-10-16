"""Sonde 3 : seuil exact, code d'erreur, et notation scientifique."""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline
from osd.adapters.oracle import _to_int

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe3"), run_id="probe3")
ad = p.source_adapter
schema = str(cfg.get("SOURCE_SCHEMA"))

print(f"_to_int('1.0369E+11') = {_to_int('1.0369E+11')}", flush=True)

for taille in (30000, 33000, 40000, 60000, 131072, 262144):
    base = "select 1 from dual where 1=1 "
    sql = base + "-- " + ("x" * max(0, taille - len(base) - 3))
    try:
        ad.scalar(sql)
        print(f"taille {len(sql):7d} : OK", flush=True)
    except BaseException as exc:
        print(f"taille {len(sql):7d} : FAIL {type(exc).__name__}", flush=True)
        print(f"    message={getattr(exc, 'message', exc)!r}", flush=True)
        print(f"    detail={getattr(exc, 'detail', '')!r}", flush=True)
print("fin", flush=True)

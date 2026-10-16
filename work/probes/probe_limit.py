"""Sonde : taille maximale d'une requete acceptee par le shell distant."""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe2"), run_id="probe2")
ad = p.source_adapter
schema = str(cfg.get("SOURCE_SCHEMA"))

# 1) nombre de segments du schema
try:
    n = ad.scalar(f"select count(*) from dba_segments where owner = '{schema}'")
    print(f"segments({schema}) = {n}", flush=True)
except BaseException as exc:
    print(f"segments: FAIL {type(exc).__name__}: {exc}", flush=True)

try:
    s = ad.scalar(f"select sum(nvl(bytes,0)) from dba_segments where owner = '{schema}'")
    print(f"sum(bytes) = {s}", flush=True)
except BaseException as exc:
    print(f"sum: FAIL {type(exc).__name__}: {exc}", flush=True)

# 2) ou se situe la limite de taille de la ligne SQL
for taille in (32768, 65536, 98304, 131072, 196608, 262144, 393216, 524288):
    base = "select 1 from dual where 1=1 "
    rembourrage = "-- " + ("x" * max(0, taille - len(base) - 5))
    sql = base + rembourrage
    try:
        ad.scalar(sql)
        print(f"taille {len(sql):7d} : OK", flush=True)
    except BaseException as exc:
        print(f"taille {len(sql):7d} : FAIL {type(exc).__name__}: {exc}",
              flush=True)
        break
print("fin", flush=True)

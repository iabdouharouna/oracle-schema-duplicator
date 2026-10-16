"""Sonde 9 : cout de la lecture des dependances intra-schema."""
import sys, time
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe9"), run_id="probe9")
ad = p.source_adapter
s = str(cfg.get("SOURCE_SCHEMA"))

t0 = time.time()
rows = ad.query(
    "select referenced_name, name from dba_dependencies "
    f"where owner = '{s}' and referenced_owner = '{s}'"
)
print(f"dependances (pull) : {time.time() - t0:.1f}s  {len(rows)} lignes",
      flush=True)
print("fin", flush=True)

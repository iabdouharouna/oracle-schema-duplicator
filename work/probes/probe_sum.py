import sys, time
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline
cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe-sum"), run_id="probe-sum")
ad = p.source_adapter
s = str(cfg.get("SOURCE_SCHEMA"))
t0 = time.time()
total = ad.schema_segment_bytes(s, content="ALL")
print(f"sans filtre : {time.time()-t0:.1f}s -> {total} octets "
      f"({total/1024**3:.2f} Gio)", flush=True)

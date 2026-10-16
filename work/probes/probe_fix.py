"""Verification de la correction sur la base reelle (lecture seule)."""
import sys, time
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline
from osd.checks.table_filter import plan_table_filter

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe-fix"), run_id="probe-fix")
ad = p.source_adapter
schema = str(cfg.get("SOURCE_SCHEMA"))

t0 = time.time()
plan = plan_table_filter(ad, schema, standalone=True)
dt = time.time() - t0
print(f"plan_table_filter : {dt:.1f}s", flush=True)
print(f"  excluded   = {len(plan.excluded)}", flush=True)
print(f"  data_only  = {len(plan.data_only)}", flush=True)
print(f"  standalone = {len(plan.standalone)}", flush=True)
print(f"  ignored    = {len(plan.ignored)}", flush=True)
print(f"  segment_ignored = {len(plan.segment_ignored)}", flush=True)
print(f"  query_lines = {len(plan.query_lines())}", flush=True)

t0 = time.time()
octets = ad.schema_segment_bytes(
    schema, content="ALL",
    segment_signatures=plan.segment_ignored,
    object_signatures=plan.ignored,
)
dt = time.time() - t0
print(f"schema_segment_bytes : {dt:.1f}s -> {octets} octets "
      f"({octets / 1024 ** 3:.1f} Gio)", flush=True)
print("fin", flush=True)

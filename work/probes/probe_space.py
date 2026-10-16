"""Sonde : coherence de l'estimation d'espace avec le plan standalone."""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import plan_table_filter
from osd.adapters.oracle import _segment_exclusions, _segment_exclu
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
ad = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe").source_adapter

t0 = time.time()
plan = plan_table_filter(ad, schema, standalone=True)
print(f"plan en {time.time()-t0:.1f}s : data_only={len(plan.data_only)} "
      f"standalone={len(plan.standalone)} seg_ignored={len(plan.segment_ignored)}")

total = ad.schema_segment_bytes(schema, content="ALL")
est = ad.schema_segment_bytes(
    schema, content="ALL",
    segment_signatures=plan.segment_ignored,
    object_signatures=plan.ignored,
)
print(f"\ntotal segments        = {total}  ({total/2**30:.2f} Gio / {total/1e9:.2f} Go)")
print(f"estimation (plan)     = {est}  ({est/2**30:.2f} Gio / {est/1e9:.2f} Go)")
print(f"retranche             = {total-est}  ({(total-est)/2**30:.2f} Gio)")

# Footprint reel des tables standalone (tous types de segments via _owned_names)
from osd.checks.table_filter import _owned_names
possede = _owned_names(ad, schema, plan.data_only)
exclus = _segment_exclusions(
    [s for s in plan.segment_ignored]
)
rows = ad.query(
    "select segment_type, segment_name, nvl(bytes,0) from dba_segments "
    f"where owner = '{schema}'"
)
foot = 0
gros = []
for r in rows:
    if len(r) < 3:
        continue
    t, n, b = str(r[0]).strip(), str(r[1]).strip(), int(float(r[2]))
    if n in possede:
        foot += b
        gros.append((b, t, n))
print(f"\nfootprint des objets des tables standalone (par nom) = "
      f"{foot} ({foot/2**30:.2f} Gio)")
for b, t, n in sorted(gros, reverse=True)[:12]:
    print(f"  {b/2**30:8.3f} Gio  {t:16s} {n}")

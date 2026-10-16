"""Sonde en lecture seule : ou et comment le plan de filtrage casse."""
import sys, time, traceback
sys.path.insert(0, "src")

from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline
from osd.checks import table_filter as tf

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
ad = p.source_adapter
schema = str(cfg.get("SOURCE_SCHEMA"))
print(f"schema={schema}", flush=True)


def step(label, fn):
    t = time.time()
    try:
        out = fn()
        print(f"[{time.time()-t:7.2f}s] {label} OK -> {out}", flush=True)
        return out
    except BaseException as exc:
        print(f"[{time.time()-t:7.2f}s] {label} FAIL {type(exc).__name__}: {exc}",
              flush=True)
        traceback.print_exc()
        return None


tables = step("_table_names", lambda: tf._table_names(ad, schema))
if tables is None:
    sys.exit(1)
tables = [str(t) for t in tables]
print(f"tables={len(tables)}  _in(tables)={len(tf._in(tables))} octets", flush=True)

# Le pire cas : mode B = tout le schema.
worst = len(tf._in(tables)) * 5
print(f"SQL _owned_names au pire = {worst} octets (x5 la liste IN)", flush=True)

iso = step("_standalone_tables", lambda: tf._standalone_tables(ad, schema))
if iso is None:
    sys.exit(2)
iso = [str(t) for t in iso]
print(f"standalone={len(iso)}  _in(standalone)={len(tf._in(iso))} octets",
      flush=True)
owned_sql = len(tf._in(iso)) * 5
print(f"SQL _owned_names pour les isolees = {owned_sql} octets", flush=True)

step("_owned_names", lambda: tf._owned_names(ad, schema, iso))
step("_signatures", lambda: tf._signatures(ad, schema, iso))
print("sonde terminee", flush=True)

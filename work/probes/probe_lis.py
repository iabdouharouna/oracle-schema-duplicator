"""Sonde ciblee : LIS_MERCHANT_DETAIL_TCR_* — standalone ou pas ?

1. appartenance au plan STANDALONE et a la fermeture FK ;
2. lignes brutes de DBA_DEPENDENCIES qui la referencent ;
3. existence dans DBA_TABLES / DBA_OBJECTS / DBA_SEGMENTS ;
4. synonymes qui pourraient porter la dependance.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import (
    plan_table_filter, _standalone_tables, _used_tables,
)
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
ad = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe").source_adapter

CIBLES = ["LIS_MERCHANT_DETAIL_TCR_0", "LIS_MERCHANT_DETAIL_TCR_1"]
liste = ", ".join(f"'{c}'" for c in CIBLES)

t0 = time.time()
standalone = set(_standalone_tables(ad, schema))
used = _used_tables(ad, schema)
print(f"resolution en {time.time()-t0:.1f}s")

print("\n== Appartenance ==")
for t in CIBLES:
    print(f"  {t:30s} standalone={t in standalone!s:5s} "
          f"utilisee={t in used!s:5s}")

print("\n== DBA_TABLES ==")
for r in ad.query(
    "select owner, table_name from dba_tables "
    f"where owner = '{schema}' and table_name like 'LIS_MERCHANT_DETAIL_TCR%'"
):
    print("  ", r)

print("\n== DBA_OBJECTS (tous types, meme prefixe) ==")
for r in ad.query(
    "select object_name, object_type, status from dba_objects "
    f"where owner = '{schema}' and object_name like 'LIS_MERCHANT_DETAIL_TCR%'"
):
    print("  ", r)

print("\n== DBA_DEPENDENCIES : lignes qui les referencent (referenced_type='TABLE') ==")
rows = ad.query(
    "select referenced_owner, referenced_name, referenced_type, "
    "       owner, name, type "
    "from dba_dependencies "
    f"where referenced_owner = '{schema}' and referenced_name in ({liste})"
)
print(f"  {len(rows)} ligne(s)")
for r in rows:
    print("  ", r)

print("\n== DBA_DEPENDENCIES : toute ligne dont referenced_name colle au motif ==")
rows = ad.query(
    "select referenced_owner, referenced_name, referenced_type, "
    "       owner, name, type "
    "from dba_dependencies "
    f"where referenced_name like 'LIS_MERCHANT_DETAIL_TCR%'"
)
print(f"  {len(rows)} ligne(s)")
for r in rows[:40]:
    print("  ", r)

print("\n== DBA_SYNONYMS pointant dessus ==")
for r in ad.query(
    "select owner, synonym_name, table_owner, table_name from dba_synonyms "
    "where table_name like 'LIS_MERCHANT_DETAIL_TCR%'"
):
    print("  ", r)

print("\n== DBA_SEGMENTS ==")
for r in ad.query(
    "select segment_name, segment_type, bytes from dba_segments "
    f"where owner = '{schema}' and segment_name like 'LIS_MERCHANT_DETAIL_TCR%' "
    "order by bytes desc"
):
    print("  ", r)

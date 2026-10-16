"""Reponses en lecture seule : R4 (fermeture des dependances dans DBA_OBJECTS)."""
import sys

sys.path.insert(0, "src")

from osd import config
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load("config/config.conf")
p = Pipeline(cfg=cfg, state=State(run_id="PROBE"), run_id="PROBE", only=[])

QUERIES = [
    ("types d'objets presents", "select distinct object_type from dba_objects order by 1"),
    ("colonnes de DBA_LOBS",
     "select column_name from dba_tab_columns where owner='SYS' "
     "and table_name='DBA_LOBS' order by column_id"),
    ("HR : tables et partitions",
     "select object_type, object_name, subobject_name from dba_objects "
     "where owner='HR' and object_type like 'TABLE%' order by 1, 2, 3"),
    ("HR : index",
     "select index_name, table_name from dba_indexes where owner='HR' order by 1"),
    ("HR : triggers",
     "select trigger_name, table_name from dba_triggers where owner='HR' order by 1"),
    ("HR : lob",
     "select table_name, column_name, segment_name, index_name from dba_lobs "
     "where owner='HR' order by 1"),
    ("HR : segments",
     "select segment_type, count(*) from dba_segments where owner='HR' "
     "group by segment_type order by 1"),
]

for libelle, sql in QUERIES:
    print("###", libelle)
    try:
        rows = p.source_adapter.query(sql)
    except Exception as exc:  # noqa: BLE001 - sonde
        print("  !!", type(exc).__name__, exc)
        continue
    for row in rows[:40]:
        print("  ", row)
    if len(rows) > 40:
        print("   ...", len(rows), "lignes")

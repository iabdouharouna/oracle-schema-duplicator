"""R4 (suite) : comment DBA_OBJECTS nomme-t-il les LOB et les partitions."""
import sys

sys.path.insert(0, "src")

from osd import config
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load("config/config.conf")
p = Pipeline(cfg=cfg, state=State(run_id="PROBE"), run_id="PROBE", only=[])

QUERIES = [
    ("objets LOB dans DBA_OBJECTS",
     "select owner, object_name, subobject_name from dba_objects "
     "where object_type='LOB' and rownum <= 5"),
    ("meme LOB cote DBA_LOBS",
     "select owner, table_name, column_name, segment_name, index_name "
     "from dba_lobs where (owner, segment_name) in "
     "(select owner, object_name from dba_objects where object_type='LOB' "
     " and rownum <= 5) and rownum <= 5"),
    ("les index LOB sont-ils des objets ?",
     "select count(*) from dba_objects where object_name in "
     "(select index_name from dba_lobs where rownum <= 20)"),
    ("partitions de table cote DBA_OBJECTS",
     "select owner, object_name, subobject_name from dba_objects "
     "where object_type='TABLE PARTITION' and rownum <= 5"),
    ("partitions d'index cote DBA_OBJECTS",
     "select owner, object_name, subobject_name from dba_objects "
     "where object_type='INDEX PARTITION' and rownum <= 5"),
]

for libelle, sql in QUERIES:
    print("###", libelle)
    try:
        rows = p.source_adapter.query(sql)
    except Exception as exc:  # noqa: BLE001 - sonde
        print("  !!", type(exc).__name__, exc)
        continue
    for row in rows[:20]:
        print("  ", row)

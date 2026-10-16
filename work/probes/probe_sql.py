"""Sondes SQL en lecture seule : structure de DBA_SEGMENTS, fermeture des
dependances, liste des tables.

Le SQL du filtre est trop compose pour n'etre verifie que par des tests
unitaires : il est execute ici tel quel contre la 19c reelle.
"""
import sys

sys.path.insert(0, "src")

from osd import config
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load("config/config.conf")
p = Pipeline(cfg=cfg, state=State(run_id="PROBE"), run_id="PROBE", only=[])
a = p.source_adapter

QUERIES = [
    ("colonnes de DBA_SEGMENTS",
     "select column_name from dba_tab_columns where owner='SYS' "
     "and table_name='DBA_SEGMENTS' order by column_id"),
    ("segments de partitions cote DBA_SEGMENTS",
     "select segment_type, segment_name from dba_segments "
     "where owner='SYS' and segment_type like '%PARTITION%' and rownum <= 6"),
    ("objets correspondants cote DBA_OBJECTS",
     "select object_type, object_name, subobject_name from dba_objects "
     "where owner='SYS' and object_type in "
     "('TABLE PARTITION','INDEX PARTITION') and rownum <= 6"),
    ("noms de tables du schema HR",
     "select object_name from dba_objects where owner = 'HR' "
     "and object_type = 'TABLE' and object_name not like 'BIN$%' "
     "order by object_name"),
]

# La fermeture des dependances, recopiee telle qu'elle sera ecrite dans
# le module de filtrage.
HR = "HR"
TABLES = "'EMPLOYEES','DEPARTMENTS'"
OWNER = HR
CLOSURE = (
    "select object_type, object_name from dba_objects "
    f"where owner = '{OWNER}' "
    "and object_type not in ('SYNONYM', 'DATABASE LINK') "
    f"and (object_name in ({TABLES}) "
    f"or object_name in (select index_name from dba_indexes "
    f"                   where owner = '{OWNER}' and table_name in ({TABLES})) "
    f"or object_name in (select trigger_name from dba_triggers "
    f"                   where owner = '{OWNER}' and table_name in ({TABLES})) "
    f"or object_name in (select segment_name from dba_lobs "
    f"                   where owner = '{OWNER}' and table_name in ({TABLES})) "
    f"or object_name in (select index_name from dba_lobs "
    f"                   where owner = '{OWNER}' and table_name in ({TABLES}))) "
    "order by object_type, object_name"
)
QUERIES.append(("fermeture EMPLOYEES+DEPARTMENTS", CLOSURE))

# Espace filtre : les segments qui restent une fois EMPLOYEES retire.
EXCL = "'EMPLOYEES','EMP_DEPARTMENT_IX','EMP_EMAIL_UK','EMP_EMP_ID_PK',"
EXCL += "'EMP_JOB_IX','EMP_MANAGER_IX','EMP_NAME_IX','DEPT_ID_PK',"
EXCL += "'DEPT_LOCATION_IX','SECURE_EMPLOYEES','UPDATE_JOB_HISTORY'"
QUERIES.append((
    "segments HR hors EMPLOYEES/DEPARTMENTS",
    "select count(*), sum(nvl(bytes,0)) from dba_segments where owner='HR' "
    "and segment_name not in (" + EXCL + ")",
))
QUERIES.append((
    "segments HR filtres (ce qu'on retire)",
    "select segment_type, segment_name, bytes from dba_segments "
    "where owner='HR' and segment_name in (" + EXCL + ") order by 1, 2",
))
QUERIES.append((
    "total HR",
    "select count(*), sum(nvl(bytes,0)) from dba_segments where owner='HR'",
))

for libelle, sql in QUERIES:
    print("###", libelle, flush=True)
    try:
        rows = a.query(sql)
    except Exception as exc:  # noqa: BLE001 - sonde
        print("  !!", type(exc).__name__, exc, flush=True)
        continue
    for row in rows[:40]:
        print("  ", row, flush=True)
    if len(rows) > 40:
        print("   ...", len(rows), "lignes", flush=True)

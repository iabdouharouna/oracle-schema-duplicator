"""Sonde lecture seule : impact d'un controle "aucun autre objet n'utilise
la table" sur la regle EXCLUDE_STANDALONE_TABLES.

On ne modifie rien : on lit le dictionnaire via l'adaptateur source.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import _standalone_tables, _table_names
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
pipe = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe")
ad = pipe.source_adapter

tables = set(_table_names(ad, schema))
isolees = set(_standalone_tables(ad, schema))
print(f"schema={schema}")
print(f"tables export={len(tables)}")
print(f"isolees FK (regle actuelle)={len(isolees)}")

# Dependances intra-schema vers une table : qui reference qui.
rows = ad.query(
    "select referenced_name, type from dba_dependencies "
    f"where owner = '{schema}' and referenced_owner = '{schema}' "
    "and referenced_type = 'TABLE'"
)
par_type = {}
tables_utilisees_par_type = {}
for r in rows:
    if len(r) < 2:
        continue
    ref = str(r[0]).strip()
    typ = str(r[1]).strip()
    par_type[typ] = par_type.get(typ, 0) + 1
    tables_utilisees_par_type.setdefault(typ, set()).add(ref)

print("\n-- dependants par type de referenceur (vers une TABLE) --")
for typ, n in sorted(par_type.items(), key=lambda kv: -kv[1]):
    touche_isolees = len(tables_utilisees_par_type[typ] & isolees)
    print(f"  {typ:20s} aretes={n:6d}  tables_distinctes={len(tables_utilisees_par_type[typ]):5d}"
          f"  dont_isolees={touche_isolees}")

# Ce que le user vise : objets "code/logique", pas la cascade propre a la table.
CODE = {
    "VIEW", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION", "PACKAGE",
    "PACKAGE BODY", "TYPE", "TYPE BODY", "TRIGGER", "SYNONYM",
}
utilisees_code = set()
for typ in CODE:
    utilisees_code |= tables_utilisees_par_type.get(typ, set())

restantes = isolees - utilisees_code
print(f"\nisolees disqualifiees par CODE={len(isolees & utilisees_code)}")
print(f"isolees restantes (CODE) = {len(restantes)}")

# Variante sans TRIGGER / SYNONYM (cascade propre a la table plutot que code).
CODE_SANS_TRIG = CODE - {"TRIGGER", "SYNONYM"}
util2 = set()
for typ in CODE_SANS_TRIG:
    util2 |= tables_utilisees_par_type.get(typ, set())
print(f"isolees restantes (sans TRIGGER/SYNONYM) = {len(isolees - util2)}")

# Un exemple pour voir le genre de referenceur.
print("\n-- echantillon de referenceurs d'une table isolee --")
for t in sorted(isolees & utilisees_code)[:5]:
    r2 = ad.query(
        "select name, type from dba_dependencies "
        f"where owner = '{schema}' and referenced_owner = '{schema}' "
        f"and referenced_type = 'TABLE' and referenced_name = '{t}'"
    )
    print(f"  {t}: " + ", ".join(f"{str(x[0]).strip()}/{str(x[1]).strip()}" for x in r2[:6]))

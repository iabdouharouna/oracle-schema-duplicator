"""Sonde : les plus grosses tables sont-elles dans le jeu STANDALONE ?

Pour chaque grande table : taille, isolement FK, lecteurs non-table.
Comparaison de l'espace des trois ensembles (FK seule, standalone, reste).
"""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import _standalone_tables, _used_tables, _table_names
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
ad = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe").source_adapter

FK = (
    "select t.table_name from dba_tables t "
    f"where t.owner = '{schema}' "
    "and not exists (select 1 from dba_constraints c "
    "where c.owner = t.owner and c.table_name = t.table_name "
    "and c.constraint_type = 'R') "
    "and not exists (select 1 from dba_constraints p "
    "join dba_constraints c "
    "on c.r_owner = p.owner and c.r_constraint_name = p.constraint_name "
    "where p.owner = t.owner and p.table_name = t.table_name "
    "and c.constraint_type = 'R')"
)

def gio(n):
    return n / 1024**3

t0 = time.time()
fk_only = {str(r[0]).strip() for r in ad.query(FK) if r}
used = _used_tables(ad, schema)
standalone = {t for t in _standalone_tables(ad, schema)}
print(f"temps de resolution : {time.time()-t0:.1f}s")
print(f"FK seules      : {len(fk_only)}")
print(f"utilisees      : {len(used)}")
print(f"standalone     : {len(standalone)}")

# Lecteurs par table (types)
lecteurs = {}
for r in ad.query(
    "select referenced_name, name, type from dba_dependencies "
    f"where referenced_owner = '{schema}' and referenced_type = 'TABLE'"
):
    if len(r) > 2:
        lecteurs.setdefault(str(r[0]).strip(), set()).add(str(r[2]).strip())

# Taille du propre segment de chaque table
tailles = {}
for r in ad.query(
    "select segment_name, bytes from dba_segments "
    f"where owner = '{schema}' and segment_type = 'TABLE'"
):
    if len(r) > 1 and r[1] is not None:
        tailles[str(r[0]).strip()] = int(float(r[1]))

tot = sum(tailles.values())
taille_fk = sum(v for k, v in tailles.items() if k in fk_only)
taille_sa = sum(v for k, v in tailles.items() if k in standalone)
print(f"\nsegments de type TABLE : {len(tailles)} tables, {gio(tot):.2f} Gio")
print(f"  dont FK seules : {gio(taille_fk):.2f} Gio")
print(f"  dont standalone: {gio(taille_sa):.2f} Gio")
print(f"  reste (non isolees) : {gio(tot - taille_fk):.2f} Gio")

print("\n-- 25 plus gros segments TABLE --")
top = sorted(tailles.items(), key=lambda kv: -kv[1])[:25]
for nom, octets in top:
    etat = ("STANDALONE" if nom in standalone
            else ("isolee FK mais LUE" if nom in fk_only else "non isolee"))
    types = ",".join(sorted(lecteurs.get(nom, set())))
    print(f"  {gio(octets):8.2f} Gio  {nom:38s} {etat:20s} {types}")

# Qui porte les 80 Gio des isolees-lues ?
lues_isolees = fk_only - standalone
gros = sorted(((tailles.get(t, 0), t) for t in lues_isolees), reverse=True)[:25]
print("\n-- 25 plus grosses tables 'isolees FK mais lues' --")
for octets, nom in gros:
    types = ",".join(sorted(lecteurs.get(nom, set())))
    print(f"  {gio(octets):8.2f} Gio  {nom:38s} {types}")

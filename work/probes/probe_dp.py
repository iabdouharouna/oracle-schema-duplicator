"""Sondes Data Pump sur la 19c reelle : R1 (separateur), R2 (syntaxe query),
R3 (INCLUDE + EXCLUDE), R5 (QUERY + PARALLEL).

Chaque sonde produit un dump et un journal nommes `osdpN.*` dans le
DIRECTORY source, plus un SQLFILE quand la relecture est utile. Tout est
supprime en fin de script (`cleanup`).
"""
import sys

sys.path.insert(0, "src")

from osd import config
from osd.adapters.datapump import DataPumpAdapter
from osd.runner import build_script, load_body
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load("config/config.conf")
p = Pipeline(cfg=cfg, state=State(run_id="PROBE"), run_id="PROBE", only=[])
side = p.source
pump = DataPumpAdapter(side, oracle=p.source_adapter)
DIRPATH = p.source_adapter.directory_path(side.directory)
print("DIRECTORY physique :", DIRPATH, flush=True)

HR = "HR"


def parfile_export(nom, extra, parallel=1):
    lines = [
        f'userid="{side.effective_connect()}"',
        f'schemas="{HR}"',
        f'directory="{side.directory}"',
        f'logfile="osdp{nom}.log"',
        'content="ALL"',
        'compression="ALL"',
        f'job_name="OSDP{nom}"',
    ]
    if parallel > 1:
        lines.append('dumpfile="osdp%s-%%d.dmp"' % nom)
        lines.append(f"parallel={parallel}")
    else:
        lines.append(f'dumpfile="osdp{nom}.dmp"')
    lines.extend(extra)
    return lines


def parfile_verify(nom):
    return [
        f'userid="{side.effective_connect()}"',
        f'directory="{side.directory}"',
        f'logfile="osdp{nom}v.log"',
        "parallel=1",
        f'dumpfile="osdp{nom}.dmp"',
        f'sqlfile="osdp{nom}v.sql"',
    ]


def remote(side_, argv, timeout=300):
    script = build_script(load_body("remote_exec.sh"), argv)
    return side_.runner.run_script(script, timeout=timeout)


def lignes_export(nom):
    res = remote(side, ["grep", "exported", f"{DIRPATH}/osdp{nom}.log"])
    return res.rows


def compte_ddl(nom, table):
    res = remote(side, ["grep", "-c", f'CREATE TABLE "{HR}"."{table}"',
                        f"{DIRPATH}/osdp{nom}v.sql"])
    return res.get("OSD_RC"), res.rows


def supprimer(*noms):
    fichiers = [f"{DIRPATH}/{n}" for n in noms if n]
    res = remote(side, ["rm", "-f"] + fichiers)
    return res.rc


def run_export(nom, extra, parallel=1):
    lignes = parfile_export(nom, extra, parallel=parallel)
    print(f"--- P{nom} parfile:", flush=True)
    for l in lignes:
        print("   ", l, flush=True)
    r = pump.run("expdp", lignes, job_name=f"OSDP{nom}", timeout=900)
    print(f"--- P{nom} rc={r.rc} erreurs={r.error_codes} avert={r.warnings} "
          f"job={r.job_status}", flush=True)
    return r


RESULTATS = {}

# ---------------------------------------------------------------- P1
# R1 : le separateur actuel du code, `;`, sur deux filtres.
r = run_export("1", ['exclude=USER;TABLE:"IN (\'EMPLOYEES\')"'])
RESULTATS["P1_excl_semicolon"] = (r.rc, r.error_codes)
print("P1 journal:", lignes_export("1")[:6], flush=True)

# ---------------------------------------------------------------- P2
# R1 : la forme documentee, une ligne `exclude=` par filtre.
r = run_export("2", ["exclude=USER", 'exclude=TABLE:"IN (\'EMPLOYEES\')"'])
RESULTATS["P2_excl_deux_lignes"] = (r.rc, r.error_codes)
print("P2 journal:", lignes_export("2")[:6], flush=True)

# ---------------------------------------------------------------- P3
# R2 : syntaxe `query=` dans un parfile, parallel=1.
r = run_export("3", ['query=HR.REGIONS:"WHERE 1=2"'])
RESULTATS["P3_query"] = (r.rc, r.error_codes)
print("P3 journal REGIONS/COUNTRIES:",
      [l for l in lignes_export("3") if "REGIONS" in l or "COUNTRIES" in l],
      flush=True)

# ---------------------------------------------------------------- P4
# R3 : INCLUDE et EXCLUDE combines sur une 19c.
r = run_export("4", ["include=TABLE", 'exclude=TABLE:"IN (\'EMPLOYEES\')"'])
RESULTATS["P4_include_exclude"] = (r.rc, r.error_codes)
print("P4 journal:", lignes_export("4")[:6], flush=True)

# ---------------------------------------------------------------- P5
# R5 : QUERY avec PARALLEL=4.
r = run_export("5", ['query=HR.REGIONS:"WHERE 1=2"'], parallel=4)
RESULTATS["P5_query_parallel"] = (r.rc, r.error_codes)
print("P5 journal REGIONS:",
      [l for l in lignes_export("5") if "REGIONS" in l],
      flush=True)

# ------------------------------------------------------------ relectures
for nom, table in (("1", "EMPLOYEES"), ("2", "EMPLOYEES"), ("4", "EMPLOYEES")):
    rv = pump.run("impdp", parfile_verify(nom), job_name=f"OSDP{nom}V", timeout=900)
    rc, rows = compte_ddl(nom, table)
    print(f"relecture P{nom} : rc={rv.rc} erreurs={rv.error_codes} | "
          f"CREATE TABLE {table} -> rc_grep={rc} {rows}", flush=True)
    RESULTATS[f"P{nom}_ddl_{table}"] = (rv.rc, rc, rows)

print("=== RESUME ===", flush=True)
for k, v in RESULTATS.items():
    print(f"{k}: {v}", flush=True)

# ------------------------------------------------------------ nettoyage
# Les dumps, journaux et SQLFILE des sondes sont supprimes du DIRECTORY :
# ce sont nos artefacts, pas des donnees.
noms = []
for nom in ("1", "2", "3", "4", "5"):
    noms += [f"osdp{nom}.dmp", f"osdp{nom}.log", f"osdp{nom}v.sql", f"osdp{nom}v.log"]
noms += ["osdp4-1.dmp", "osdp4-2.dmp", "osdp4-3.dmp", "osdp4-4.dmp",
         "osdp5-1.dmp", "osdp5-2.dmp", "osdp5-3.dmp", "osdp5-4.dmp"]
print("nettoyage rc =", supprimer(*noms), flush=True)

# Tables maitres eventuellement laissees par un job en echec.
res = p.source_adapter.query(
    "select table_name from dba_tables where owner='HR' "
    "and (table_name like 'OSDP%' or table_name like 'SYS_EXPORT%') order by 1"
)
print("tables maitres restantes dans HR :", res, flush=True)

# Jobs Data Pump encore connus.
res = p.source_adapter.query(
    "select job_name, operation, state from dba_datapump_jobs "
    "where owner='HR' order by 1"
)
print("jobs Data Pump HR :", res, flush=True)

"""Sondes de suite : R3 isole (INCLUDE seul vs INCLUDE+EXCLUDE)."""
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


def remote(argv, timeout=300):
    script = build_script(load_body("remote_exec.sh"), argv)
    return side.runner.run_script(script, timeout=timeout)


def journal(nom):
    res = remote(["grep", "-n", "-E", "ORA-|LRM-", f"{DIRPATH}/osdp{nom}.log"])
    if res.rc == 0:
        return res.rows[:10]
    # pas d'erreur dans le journal : dernieres lignes pour contexte
    res = remote(["tail", "-12", f"{DIRPATH}/osdp{nom}.log"])
    return res.rows[:12]


def exp(nom, extra, parallel=1):
    lines = [
        f'userid="{side.effective_connect()}"',
        'schemas="HR"',
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
    print(f"--- P{nom} : {extra}", flush=True)
    r = pump.run("expdp", lines, job_name=f"OSDP{nom}", timeout=900)
    print(f"    rc={r.rc} erreurs={r.error_codes}", flush=True)
    print("    journal :", journal(nom), flush=True)
    return r


# R3.a : INCLUDE seul
exp("6", ["include=TABLE"])
# R3.b : INCLUDE + EXCLUDE, la vraie erreur
exp("7", ["include=TABLE", 'exclude=TABLE:"IN (\'EMPLOYEES\')"'])

# Nettoyage : nos artefacts de sonde.
noms = [f"osdp{n}.{ext}" for n in ("6", "7") for ext in ("dmp", "log")]
print("nettoyage rc =", remote(["rm", "-f"] + [f"{DIRPATH}/{n}" for n in noms]).rc,
      flush=True)
restes = p.source_adapter.query(
    "select table_name from dba_tables where owner='HR' "
    "and (table_name like 'OSDP%' or table_name like 'SYS_EXPORT%') order by 1"
)
print("tables maitres restantes :", restes, flush=True)

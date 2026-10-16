"""Sonde 7 : le parfile du mode B (5634 lignes, ~250 Ko) survit-il ?

Reproduit exactement la forme de `DataPumpAdapter._script_with_parfile` :
une variable `osd_parcontent=<contenu multi-ligne entre simples quotes>`,
puis un corps qui l'ecrit et le relit. Le corps n'est pas Data Pump : on
veut seulement savoir si le script distant va au bout.
"""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline
from osd.runner import build_script, Raw, load_body, _single_quote

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe7"), run_id="probe7")
ad = p.source_adapter
runner = ad.side.runner

# Parfile du mode B, tel que le produirait le plan de filtrage.
lignes = [f'PCARDPROCBO.TABLE{i}:"WHERE 1=2"' for i in range(5634)]
contenu = "\n".join(lignes)
print(f"contenu = {len(contenu)} octets, {len(lignes)} lignes, "
      f"ligne max = {max(len(x) for x in lignes)}", flush=True)

parpath = "/tmp/osd-probe-parfile.txt"
bootstrap = "\n".join([
    f"osd_parpath={_single_quote(parpath)}",
    f"osd_parcontent={_single_quote(contenu)}",
    '(umask 077 && printf %s "$osd_parcontent" > "$osd_parpath") \\',
    "    || osd_die 'ecriture du parfile impossible' 73",
    'printf "LIGNES=%s OCTETS=%s\\n" "$(wc -l < "$osd_parpath")" '
    '"$(wc -c < "$osd_parpath")"',
    f"rm -f {_single_quote(parpath)}",
])
corps = load_body("remote_sqlplus.sh")
script = build_script(corps, [ad.side.connect, "select 1 from dual"],
                      bootstrap=bootstrap, env=ad.side.env())
print(f"script = {len(script)} octets, ligne max = "
      f"{max(len(x) for x in script.splitlines())}", flush=True)
try:
    res = runner.run_script(script, timeout=60, mutating=True)
    print(f"rc={res.rc}", flush=True)
    print("--- stdout ---", flush=True)
    print((res.stdout_raw or "")[:2000], flush=True)
    print("--- stderr ---", flush=True)
    print((res.stderr or "")[:2000], flush=True)
    for ligne in (res.stdout_raw or "").splitlines():
        if "LIGNES=" in ligne:
            print(ligne.strip(), flush=True)
except BaseException as exc:
    print(f"FAIL {type(exc).__name__}: {getattr(exc, 'message', exc)}", flush=True)
print("fin", flush=True)

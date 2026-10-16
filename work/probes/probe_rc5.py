"""Preuve in vivo : un script qui sort en 5 doit DELIVRER son bloc.

Avant la correction, Ansible lisait le 5 de sshpass comme « mot de
passe incorrect », declarait l'hote injoignable et jetait la sortie.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")

from osd import config as config_mod
from osd.adapters import ansible_runner
from osd.errors import OsdError
from osd.runner import build_script

cfg = config_mod.load(Path("config/config.conf"))
runner = ansible_runner.AnsibleRunner(
    str(cfg.get("SOURCE_HOST")),
    inventory=str(cfg.get("OSD_INVENTORY")),
    group=ansible_runner.GROUP_SOURCE,
    vault_password_file=str(cfg.get("OSD_VAULT_PASSWORD_FILE")),
    side="source",
)

bloc = build_script("osd_kv OSD_PROBE 1\nosd_exit 5\n", ["x"], env={})
try:
    res = runner.run_script(bloc, timeout=30)
except OsdError as exc:
    print("ECHEC: %s: %s" % (type(exc).__name__, exc))
    print("detail: %r" % (exc.detail,))
    sys.exit(1)

print("pas d'exception (la sortie a ete delivree)")
print("rc       =", res.rc)
print("OSD_PROBE=", res.get("OSD_PROBE"))
print("marqueur =", "OSD_RESULT_END rc=5" in res.stdout_raw)
print(
    "verdict  =",
    "OK" if (res.rc == 5 and res.get("OSD_PROBE") == "1") else "INATTENDU",
)
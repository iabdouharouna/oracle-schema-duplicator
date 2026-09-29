#!/usr/bin/env python3
"""Verifie le protocole distant contre l'instance Oracle locale.

Controle de fumee du jalon 2 : le script POSIX est bien transmis, le bloc
de resultat est bien parse, et une requete SQL reellement executee
retourne des donnees exploitables. Utilise par `tests/integration`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from osd.adapters.oracle import OracleAdapter, OracleSide  # noqa: E402
from osd.runner import LocalRunner, RemoteRunner, build_script, load_body  # noqa: E402


def check_block():
    print("== 1. Bloc de resultat, commande simple ==")
    runner = LocalRunner()
    script = build_script(load_body("remote_exec.sh"), ["/bin/echo", "bonjour-osd"])
    res = runner.run_script(script, timeout=30)
    print("   rc       =", res.rc)
    print("   rows     =", res.rows)
    print("   kv       =", res.kv)
    assert res.rc == 0, res
    assert "bonjour-osd" in " ".join(res.rows), res.rows
    print("   OK\n")


def check_no_eval():
    print("== 2. Un argument hostile reste litteral ==")
    runner = LocalRunner()
    hostile = "a; touch /tmp/osd-pwned; echo $(id) `id` |b"
    script = build_script(load_body("remote_exec.sh"), ["/bin/echo", hostile])
    res = runner.run_script(script, timeout=30)
    print("   rc       =", res.rc)
    print("   rows     =", res.rows)
    assert res.rc == 0, res
    assert hostile in " ".join(res.rows), res.rows
    assert not Path("/tmp/osd-pwned").exists(), "INJECTION REUSSIE"
    print("   OK: aucun effet de bord\n")


def check_space():
    print("== 3. Espace disque (remote_space) ==")
    runner = LocalRunner()
    script = build_script(load_body("remote_space.sh"), ["/data"])
    res = runner.run_script(script, timeout=30)
    print("   rc       =", res.rc)
    for key in ("OSD_TOTAL_BYTES", "OSD_USED_BYTES", "OSD_AVAIL_BYTES", "OSD_USED_PERCENT"):
        print(f"   {key:22}= {res.kv.get(key)}")
    assert res.rc == 0, res
    assert int(res.kv["OSD_AVAIL_BYTES"]) > 0
    print("   OK\n")


def check_oracle():
    print("== 4. SQL*Plus reel ==")
    side = OracleSide(
        name="source",
        connect="//172.16.254.50:1522/OEMCC",
        schema="HR",
        directory="DATA_PUMP_DIR",
        user="oracle",
        runner=LocalRunner(),
    )
    adp = OracleAdapter(side)
    info = adp.check_connection()
    for key in sorted(info):
        print(f"   {key:16}= {info[key][:70]}")
    assert "19" in info.get("version", ""), info
    assert info.get("status") == "OPEN", info
    print("   OK\n")


def check_directories():
    print("== 5. Objets DIRECTORY ==")
    side = OracleSide(
        name="source", connect="//172.16.254.50:1522/OEMCC", schema="HR",
        directory="DATA_PUMP_DIR", user="oracle", runner=LocalRunner(),
    )
    adp = OracleAdapter(side)
    path = adp.directory_path("DATA_PUMP_DIR")
    print("   DATA_PUMP_DIR ->", path)
    assert path, "DIRECTORY introuvable"
    print("   OK\n")


def main():
    check_block()
    check_no_eval()
    check_space()
    check_oracle()
    check_directories()
    print("TOUT EST VERT")
    return 0


if __name__ == "__main__":
    sys.exit(main())

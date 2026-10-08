"""Tests de la creation du compte cible a l'image de la source.

`plan_create_target_schema` est une fonction pure : elle lit des
metadonnees via un adaptateur et rend du SQL, sans jamais executer. Ces
tests portent donc sur ce qu'elle **decide** : quel DDL, quels
avertissements, et quelles situations elle refuse. Ils ne dependent ni
d'une base, ni d'un runner.

Le point sensible est le mot de passe : la forme `IDENTIFIED BY VALUES`
reprend l'empreinte du compte source. Un test verifie qu'elle reste
masquee par le redacteur, car c'est un secret exploitable hors ligne.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

import support  # noqa: F401

from osd import exit_codes as ec
from osd.checks.schema import plan_create_target_schema
from osd.errors import PrereqError
from osd.redact import redact


#: Reponses source par defaut : un compte HR complet.
_SOURCE = {
    "from dba_users where username": [["USERS", "TEMP", "DEFAULT"]],
    "select spare4 from sys.user$": [["S:B031DD;T:D80021"]],
    "from dba_sys_privs where grantee": [["CREATE SESSION"], ["CREATE TABLE"]],
    "from dba_role_privs where grantee": [["RESOURCE"]],
    "from dba_ts_quotas where username": [["USERS", "-1"]],
}

#: Reponses cible par defaut : les tablespaces et le profil existent.
_TARGET = {
    "from dba_tablespaces": [["USERS"], ["TEMP"]],
    "select distinct profile from dba_profiles": [["DEFAULT"]],
    "select role from dba_roles": [["RESOURCE"]],
}


def _source(**extra: Any):
    responses: Dict[str, Any] = dict(_SOURCE)
    responses.update(extra.pop("responses", None) or {})
    return support.FakeAdapter(responses=responses, **extra)


def _target(**extra: Any):
    responses: Dict[str, Any] = dict(_TARGET)
    responses.update(extra.pop("responses", None) or {})
    return support.FakeAdapter(responses=responses, **extra)


def _plan(source=None, target=None, **kw):
    return plan_create_target_schema(
        source or _source(),
        target or _target(),
        source_schema=kw.pop("source_schema", "HR"),
        target_schema=kw.pop("target_schema", "HR2"),
        **kw,
    )


class TestDDLDeCreation(unittest.TestCase):
    def test_le_compte_est_cree_a_l_image_de_la_source(self):
        plan = _plan()
        ddl = "\n".join(plan.statements)
        self.assertIn("create user HR2", ddl)
        self.assertIn("identified by values 'S:B031DD;T:D80021'", ddl)
        self.assertIn("default tablespace USERS", ddl)
        self.assertIn("temporary tablespace TEMP", ddl)
        self.assertIn("profile DEFAULT", ddl)
        self.assertIn("quota unlimited on USERS", ddl)
        self.assertIn("grant CREATE SESSION, CREATE TABLE to HR2", ddl)
        self.assertIn("grant RESOURCE to HR2", ddl)

    def test_l_empreinte_du_mot_de_passe_est_masquee_par_le_redacteur(self):
        """Le verifier ne doit jamais apparaitre en clair dans un journal."""
        ddl = "\n".join(_plan().statements)
        masque = redact(ddl)
        self.assertNotIn("S:B031DD", masque)
        self.assertNotIn("T:D80021", masque)

    def test_un_quota_chiffre_est_repris_tel_quel(self):
        source = _source(responses={
            "from dba_ts_quotas where username": [["USERS", "10485760"]],
        })
        self.assertIn("quota 10485760 on USERS", "\n".join(_plan(source).statements))

    def test_le_remap_redirige_le_tablespace_par_defaut(self):
        target = _target(responses={"from dba_tablespaces": [["UTILITY"], ["TEMP"]]})
        plan = _plan(target=target, remap_tablespace=["USERS:UTILITY"])
        ddl = "\n".join(plan.statements)
        self.assertIn("default tablespace UTILITY", ddl)
        self.assertIn("quota unlimited on UTILITY", ddl)


class TestAvertissementsEtReplis(unittest.TestCase):
    def test_un_profil_absent_replie_sur_default(self):
        target = _target(responses={
            "select distinct profile from dba_profiles": [["DEFAULT"]],
        })
        source = _source(responses={
            "from dba_users where username": [["USERS", "TEMP", "APP_PROFILE"]],
        })
        plan = _plan(source=source, target=target)
        self.assertIn("profile DEFAULT", "\n".join(plan.statements))
        self.assertTrue(any("APP_PROFILE" in w for w in plan.warnings))

    def test_un_role_absent_est_ignore_avec_avertissement(self):
        source = _source(responses={
            "from dba_role_privs where grantee": [["RESOURCE"], ["APPLI_ROLE"]],
        })
        target = _target(responses={"select role from dba_roles": [["RESOURCE"]]})
        plan = _plan(source=source, target=target)
        self.assertNotIn("APPLI_ROLE", "\n".join(plan.statements))
        self.assertTrue(any("APPLI_ROLE" in w for w in plan.warnings))


class TestRefus(unittest.TestCase):
    def test_un_tablespace_par_defaut_absent_est_un_code_prerequis(self):
        target = _target(responses={"from dba_tablespaces": [["TEMP"]]})
        with self.assertRaises(PrereqError) as ctx:
            _plan(target=target)
        self.assertEqual(ctx.exception.code, ec.PREREQ)
        self.assertIn("REMAP_TABLESPACE", ctx.exception.hint or "")

    def test_un_tablespace_temporaire_absent_est_un_code_prerequis(self):
        target = _target(responses={"from dba_tablespaces": [["USERS"]]})
        with self.assertRaises(PrereqError) as ctx:
            _plan(target=target)
        self.assertEqual(ctx.exception.code, ec.PREREQ)
        self.assertIn("temporaire", ctx.exception.message)

    def test_une_empreinte_illisible_est_un_code_prerequis(self):
        source = _source(responses={"select spare4 from sys.user$": []})
        with self.assertRaises(PrereqError) as ctx:
            _plan(source=source)
        self.assertEqual(ctx.exception.code, ec.PREREQ)
        self.assertIn("SYSDBA", ctx.exception.hint or "")

    def test_un_compte_cible_verrouille_est_un_code_prerequis(self):
        target = _target(responses={
            "from dba_users where username": [["LOCKED"]],
        })
        with self.assertRaises(PrereqError) as ctx:
            _plan(target=target)
        self.assertEqual(ctx.exception.code, ec.PREREQ)
        self.assertIn("LOCKED", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
"""Base de test : on recrée une base vierge depuis les scripts initdb (schéma + plan comptable + activation)."""
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INITDB = ROOT.parent / "initdb"
SCRIPTS = ["01-schema.sql", "03-plan-comptable.sql", "04-activer-plan-comptable.sql"]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    admin = os.environ.get("TEST_ADMIN_URL")
    if not admin:
        pytest.skip("TEST_ADMIN_URL non défini")
    name = "t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    url = admin.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(url, autocommit=True) as c:
        for s in SCRIPTS:
            c.execute((INITDB / s).read_text(encoding="utf-8"))
    from compta_agent import config
    monkeypatch.setattr(config, "INVOICES_DIR", tmp_path / "invoices")
    if os.environ.get("TEST_AS_AGENT_APP"):
        # Rejoue 02-roles.sh puis se connecte avec le compte restreint, comme en production
        sh = (INITDB / "02-roles.sh").read_text(encoding="utf-8")
        sql = sh.split("<<EOSQL", 1)[1].split("EOSQL", 1)[0]
        sql = (sql.replace("${AGENT_DB_PASSWORD}", "x").replace("${STUDIO_DB_PASSWORD}", "y")
                  .replace("${POSTGRES_DB}", name))
        for role in ("agent_app", "studio_ro"):    # les rôles sont communs à tout le serveur : créer une seule fois
            pw = "x" if role == "agent_app" else "y"
            sql = sql.replace(f"create role {role} login password '{pw}';",
                              f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') "
                              f"then create role {role} login password '{pw}'; end if; end $$;")
        with psycopg.connect(url, autocommit=True) as su:
            su.execute(sql)
        url = url.replace("//postgres@", "//agent_app@")
    c = psycopg.connect(url, row_factory=dict_row)
    c.execute("select set_config('app.actor', 'test', false)")
    c.commit()
    yield c
    c.close()
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'drop database "{name}" with (force)')


@pytest.fixture()
def make_file(tmp_path):
    def _mk(name="facture.jpg", content=None):
        p = tmp_path / name
        p.write_bytes(content or name.encode() + b"-contenu")
        return p
    return _mk

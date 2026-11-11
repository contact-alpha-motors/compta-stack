"""Plan comptable : activation unitaire et import en masse depuis la feuille de correspondance."""
import pytest

from compta_agent import accounts


def _status(conn, code):
    row = conn.execute("select status, label, usage_hint from accounts where code=%s", (code,)).fetchone()
    return row


def test_le_plan_odoo_est_importe_en_pending_avec_un_sous_ensemble_actif(conn):
    total = conn.execute("select count(*) n from accounts").fetchone()["n"]
    actifs = conn.execute("select count(*) n from accounts where status='active'").fetchone()["n"]
    assert total > 1000                      # plan Odoo complet importé
    assert 40 < actifs < 200                 # seul le sous-ensemble mappé est actif
    # un compte-rôle est bien actif, un compte ambigu reste pending
    assert _status(conn, "401100")["status"] == "active"
    assert _status(conn, "231100")["status"] == "pending"


def test_activation_unitaire(conn):
    accounts.activate(conn, "231100", label="Bâtiments industriels", usage_hint="construction")
    r = _status(conn, "231100")
    assert r["status"] == "active" and r["label"] == "Bâtiments industriels" and r["usage_hint"] == "construction"


def test_desactivation(conn):
    accounts.disable(conn, "401100")
    assert _status(conn, "401100")["status"] == "rejected"


def test_compte_absent_leve_une_erreur(conn):
    with pytest.raises(ValueError):
        accounts.activate(conn, "000000")


def test_import_mapping_active_les_lignes_renseignees(conn, tmp_path):
    csv_path = tmp_path / "mapping.csv"
    csv_path.write_text(
        "code_syscohada;libelle_fr;usage_hint;odoo_propose;statut;odoo_candidats\n"
        "231;Bâtiments;construction de local;231200;a_confirmer;231100;231200\n"
        "999;Rien;;;a_confirmer;\n",          # ligne sans odoo_propose : ignorée
        encoding="utf-8-sig",
    )
    n = accounts.import_mapping(conn, str(csv_path))
    assert n == 1
    r = _status(conn, "231200")
    assert r["status"] == "active" and r["label"] == "Bâtiments" and r["usage_hint"] == "construction de local"

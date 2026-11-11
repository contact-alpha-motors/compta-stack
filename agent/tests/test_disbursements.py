"""Module B — canal A (sortie de caisse) et canal B (ordre de virement banque)."""
import os
import uuid
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from compta_agent import disbursements as d
from compta_agent.categorize import Categorizer


def seed(conn):
    ws = conn.execute("insert into workspaces(name) values ('Alpha') returning id").fetchone()["id"]
    def user(ext, name, role):
        return str(conn.execute(
            "insert into users(workspace_id, external_id, full_name, role) values (%s,%s,%s,%s) returning id",
            (ws, ext, name, role)).fetchone()["id"])
    ids = {
        "ws": str(ws),
        "demandeur": user("u1", "Demandeur", "requester"),
        "comptable": user("u2", "Comptable", "admin"),     # admin = comptable
        "manager": user("u3", "Manager", "manager"),
        "autre": user("u4", "Autre", "requester"),
    }
    conn.commit()
    return ids


class FakeCaisse:
    def __init__(self):
        self.last = None

    def resolve_employee(self, name):
        from compta_agent.assistant_caisse import AssistantCaisseError
        if name and ("laurent" in name.lower() or "avigail" in name.lower()):
            return {"id": "emp-1", "eligible_virement": True}
        raise AssistantCaisseError(f"« {name} » non éligible")

    def create_virement(self, employee_id, montant, motif=None, virement_date=None, compte_emetteur="alpha_motors"):
        self.last = dict(employee_id=employee_id, montant=montant, compte_emetteur=compte_emetteur, motif=motif)
        return {"id": "vir-9", "reference": "REF#9", "pdf_url": "https://proj.supabase.co/signed/pdf"}

    def download_pdf(self, url):
        return b"%PDF-1.4 ordre"


def _lines(conn, entry_id):
    return conn.execute("select account_code, debit, credit from entry_lines where entry_id=%s order by debit desc",
                        (entry_id,)).fetchall()


def a_read_document(conn, montant, tva=0):
    extracted = {"emetteur": "Garage", "date": "2026-10-21", "libelle": "Pièces", "montant_total": montant,
                 "tva": tva, "mode_paiement": "especes", "lisibilite": 1, "devise": "XOF"}
    return str(conn.execute(
        "insert into documents(storage_path, sha256, status, extracted) values ('/j', %s, 'extracted', %s) returning id",
        (uuid.uuid4().hex, Jsonb(extracted))).fetchone()["id"])


# ---------------------------------------------------------------- Canal A : sortie de caisse

def _approved_cash(conn, u, amount=50000):
    r = d.create_request(conn, u["demandeur"], amount, "Achat pièces", method="especes")
    d.approve(conn, r.request_id, u["comptable"])
    return r.request_id


def test_demande_de_caisse_est_submitted_sans_piece(conn):
    u = seed(conn)
    r = d.create_request(conn, u["demandeur"], 50000, "Carburant")
    assert r.status == "submitted"       # plus de pièce exigée en amont


def test_le_demandeur_ne_valide_pas_sa_demande(conn):
    u = seed(conn)
    r = d.create_request(conn, u["demandeur"], 50000, "Carburant")
    with pytest.raises(ValueError, match="Séparation"):
        d.approve(conn, r.request_id, u["demandeur"])


def test_un_simple_demandeur_ne_peut_pas_valider(conn):
    u = seed(conn)
    r = d.create_request(conn, u["demandeur"], 50000, "Carburant")
    with pytest.raises(ValueError, match="non autorisé"):
        d.approve(conn, r.request_id, u["autre"])


def test_comptable_valide_puis_manager_decaisse(conn):
    u = seed(conn)
    rid = _approved_cash(conn, u)
    out = d.disburse(conn, rid, u["manager"])
    assert out.status == "paid"
    req = d.get_request(conn, rid)
    assert str(req["disbursed_by"]) == u["manager"] and req["justif_due_at"]
    # écriture d'avance : Débit 421 / Crédit 571
    assert [(l["account_code"], l["debit"], l["credit"]) for l in _lines(conn, req["entry_id"])] == [
        ("421100", 50000, 0), ("571100", 0, 50000)]


def test_decaissement_exige_une_demande_validee(conn):
    u = seed(conn)
    r = d.create_request(conn, u["demandeur"], 50000, "Carburant")
    with pytest.raises(ValueError, match="validée"):
        d.disburse(conn, r.request_id, u["manager"])


def test_rejet_avec_motif(conn):
    u = seed(conn)
    r = d.create_request(conn, u["demandeur"], 50000, "Carburant")
    out = d.reject(conn, r.request_id, u["comptable"], "hors budget")
    assert out.status == "rejected" and d.get_request(conn, r.request_id)["rejected_reason"] == "hors budget"


# ---------------------------------------------------------------- Canal B : ordre de virement (banque)

def test_ordre_de_virement_direct_par_manager(conn):
    u = seed(conn)
    caisse = FakeCaisse()
    r = d.create_order(conn, u["manager"], 100000, "Laurent Ondobo", "approvisionnement caisse", caisse)
    assert r.status == "order_created"
    req = d.get_request(conn, r.request_id)
    assert req["payment_method"] == "virement" and req["external_order_ref"] == "vir-9"
    assert req["order_pdf_path"] and os.path.exists(req["order_pdf_path"])


def test_ordre_de_virement_interdit_au_demandeur(conn):
    u = seed(conn)
    with pytest.raises(ValueError, match="non autorisé"):
        d.create_order(conn, u["demandeur"], 100000, "Laurent Ondobo", "x", FakeCaisse())


def test_ordre_de_virement_beneficiaire_non_eligible(conn):
    u = seed(conn)
    with pytest.raises(Exception, match="éligible"):
        d.create_order(conn, u["manager"], 100000, "Inconnu", "x", FakeCaisse())


def test_confirmation_de_reception_passe_lentree_en_caisse(conn):
    u = seed(conn)
    r = d.create_order(conn, u["manager"], 100000, "Laurent Ondobo", "appro", FakeCaisse())
    out = d.confirm_reception(conn, r.request_id, u["comptable"])
    assert out.status == "received"
    req = d.get_request(conn, r.request_id)
    assert [(l["account_code"], l["debit"], l["credit"]) for l in _lines(conn, req["entry_id"])] == [
        ("571100", 100000, 0), ("521001", 0, 100000)]   # Débit caisse / Crédit banque


def test_receptions_en_attente_listees(conn):
    u = seed(conn)
    d.create_order(conn, u["manager"], 100000, "Laurent Ondobo", "appro", FakeCaisse())
    assert len(d.pending_receptions(conn)) == 1


# ---------------------------------------------------------------- Justificatifs & relances

def _paid_cash(conn, u, amount=50000):
    rid = _approved_cash(conn, u, amount)
    d.disburse(conn, rid, u["manager"])
    return rid


def _overdue(conn, rid):
    conn.execute("update disbursement_requests set justif_due_at = now() - interval '1 day' where id::text like %s",
                 (rid + "%",))
    conn.commit()


def test_rapprochement_exact(conn):
    u = seed(conn)
    rid = _paid_cash(conn, u, 50000)
    out = d.reconcile(conn, rid, a_read_document(conn, 50000), Categorizer(lambda a, dt: {"code": "605200", "confiance": 0.9, "raison": "t"}))
    assert out.status == "justified" and d.get_request(conn, rid)["remainder"] == 0


def test_rapprochement_reliquat(conn):
    u = seed(conn)
    rid = _paid_cash(conn, u, 50000)
    d.reconcile(conn, rid, a_read_document(conn, 30000), Categorizer(lambda a, dt: {"code": "605200", "confiance": 0.9, "raison": "t"}))
    assert d.get_request(conn, rid)["remainder"] == 20000


def test_relances_recepteur_et_decaisseur_puis_escalade(conn, monkeypatch):
    from compta_agent import config
    monkeypatch.setattr(config, "JUSTIF_MAX_REMINDERS", 2)
    u = seed(conn)
    rid = _paid_cash(conn, u)           # demandeur = récepteur, manager = décaisseur
    _overdue(conn, rid)
    a1 = d.run_reminders(conn)
    assert len([x for x in a1 if x["action"] == "reminder"]) == 2    # récepteur + décaisseur
    d.run_reminders(conn)                                            # 2e tour
    a3 = d.run_reminders(conn)
    assert any(x["action"] == "escalate" for x in a3)
    assert d.run_reminders(conn) == []                               # déjà escaladé


def test_demandeur_en_retard_signale_a_la_validation(conn):
    u = seed(conn)
    _overdue(conn, _paid_cash(conn, u))
    r2 = d.create_request(conn, u["demandeur"], 20000, "Autre")
    out = d.approve(conn, r2.request_id, u["comptable"])
    assert any("retard" in f for f in out.flags)

from datetime import date
from decimal import Decimal

import pytest

from compta_agent import pipeline, reports, rules
from compta_agent.categorize import Categorizer

TODAY = date(2026, 10, 20)


class FakeExtractor:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def extract(self, path):
        self.calls += 1
        return dict(self.raw)


def chooser(code="605200", conf=0.9):
    return Categorizer(lambda accounts, data: {"code": code, "confiance": conf, "raison": "test"})


CIE = {"emetteur": "CIE", "date": "2026-10-15", "libelle": "Facture d'électricité de septembre",
       "montant_total": 118000, "tva": 18000, "mode_paiement": "non_paye", "lisibilite": 0.9, "devise": "XOF"}


def lines(conn, entry_id):
    return conn.execute("select account_code, debit, credit from entry_lines where entry_id=%s order by debit desc, account_code",
                        (entry_id,)).fetchall()


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw,expected", [
    ("118 000", 118000), ("118 000 FCFA", 118000), ("1.250.000", 1250000), ("1,250,000.50", Decimal("1250000.50")),
    ("12 500,75", Decimal("12500.75")), (45000, 45000), (None, None), ("abc", None), ("12.500", 12500),
])
def test_parse_amount(raw, expected):
    assert pipeline.parse_amount(raw) == (Decimal(expected) if expected is not None else None)


def test_parse_date():
    assert pipeline.parse_date("03/04/2026") == date(2026, 4, 3)      # jour/mois/année
    assert pipeline.parse_date("2026-04-03") == date(2026, 4, 3)
    assert pipeline.parse_date("n'importe quoi") is None


# ---------------------------------------------------------------- pipeline

def test_facture_avec_tva_donne_une_ecriture_equilibree(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser(), today=TODAY)
    assert r.status == "entry_proposed" and r.proposal.code == "605200"
    ls = lines(conn, r.entry_id)
    assert [(l["account_code"], l["debit"], l["credit"]) for l in ls] == [
        ("605200", Decimal("100000.00"), 0), ("445200", Decimal("18000.00"), 0), ("401100", 0, Decimal("118000.00"))]
    e = conn.execute("select status, journal_code from entries where id=%s", (r.entry_id,)).fetchone()
    assert e["status"] == "draft" and e["journal_code"] == "AC"       # jamais validé automatiquement


def test_service_utilise_le_compte_de_tva_des_services(conn, make_file):
    raw = {**CIE, "emetteur": "Agence Voyages", "libelle": "Billet d'avion mission", "mode_paiement": "non_paye"}
    r = pipeline.ingest(conn, make_file("service.jpg"), FakeExtractor(raw), chooser("618100"), today=TODAY)
    assert {l["account_code"] for l in lines(conn, r.entry_id)} == {"618100", "445400", "401100"}


def test_sans_tva_tout_va_a_la_charge(conn, make_file):
    raw = {**CIE, "tva": None}
    r = pipeline.ingest(conn, make_file(), FakeExtractor(raw), chooser(), today=TODAY)
    assert [(l["account_code"], l["debit"], l["credit"]) for l in lines(conn, r.entry_id)] == [
        ("605200", Decimal("118000.00"), 0), ("401100", 0, Decimal("118000.00"))]


def test_paiement_especes_va_en_caisse(conn, make_file):
    raw = {**CIE, "tva": None, "mode_paiement": "especes", "libelle": "Carburant", "emetteur": "Total"}
    r = pipeline.ingest(conn, make_file(), FakeExtractor(raw), chooser("605300"), today=TODAY)
    assert {l["account_code"] for l in lines(conn, r.entry_id)} == {"605300", "571100"}
    assert conn.execute("select journal_code from entries where id=%s", (r.entry_id,)).fetchone()["journal_code"] == "CA"


def test_champ_manquant_pose_une_question_et_cree_aucune_ecriture(conn, make_file):
    raw = {**CIE, "date": None, "libelle": None, "question": "Quelle est la date et l'objet de cette facture ?"}
    r = pipeline.ingest(conn, make_file(), FakeExtractor(raw), chooser(), today=TODAY)
    assert r.status == "needs_clarification" and "date" in r.message.lower()
    assert conn.execute("select count(*) n from entries").fetchone()["n"] == 0
    d = conn.execute("select status, clarification from documents").fetchone()
    assert d["status"] == "needs_clarification" and d["clarification"]


def test_photo_floue_pose_une_question(conn, make_file):
    raw = {**CIE, "lisibilite": 0.2}
    r = pipeline.ingest(conn, make_file(), FakeExtractor(raw), chooser(), today=TODAY)
    assert r.status == "needs_clarification"


def test_devise_etrangere_est_refusee(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor({**CIE, "devise": "EUR"}), chooser(), today=TODAY)
    assert r.status == "needs_clarification" and "EUR" in r.message


def test_date_future_est_refusee(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor({**CIE, "date": "2027-01-01"}), chooser(), today=TODAY)
    assert r.status == "needs_clarification"


def test_meme_fichier_deux_fois_est_un_doublon(conn, make_file):
    f = make_file()
    ex = FakeExtractor(CIE)
    pipeline.ingest(conn, f, ex, chooser(), today=TODAY)
    r2 = pipeline.ingest(conn, f, ex, chooser(), today=TODAY)
    assert r2.status == "duplicate" and ex.calls == 1       # pas de 2e appel au modèle


def test_doublon_probable_est_signale(conn, make_file):
    pipeline.ingest(conn, make_file("a.jpg"), FakeExtractor(CIE), chooser(), today=TODAY)
    r2 = pipeline.ingest(conn, make_file("b.jpg"), FakeExtractor(CIE), chooser(), today=TODAY)
    assert r2.status == "entry_proposed" and any("doublon" in w for w in r2.warnings)


def test_fix_complete_une_lecture_incomplete(conn, make_file):
    raw = {**CIE, "date": None}
    r = pipeline.ingest(conn, make_file(), FakeExtractor(raw), chooser(), today=TODAY)
    assert r.status == "needs_clarification"
    r2 = pipeline.fix(conn, r.doc_id, {"date": "2026-10-14"}, chooser(), today=TODAY)
    assert r2.status == "entry_proposed"
    assert conn.execute("select entry_date from entries where id=%s", (r2.entry_id,)).fetchone()["entry_date"] == date(2026, 10, 14)


def test_sans_categorie_le_document_est_extrait_sans_ecriture(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), Categorizer(None), today=TODAY)
    assert r.status == "extracted" and r.entry_id is None
    r2 = pipeline.categorize_manual(conn, r.doc_id, "605200")
    assert r2.status == "entry_proposed"


def test_llm_ne_peut_pas_choisir_un_compte_hors_liste(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser("9999"), today=TODAY)
    assert r.status == "extracted"                           # compte inventé : refusé


# ---------------------------------------------------------------- règles et apprentissage

def test_regle_validee_est_appliquee_sans_modele(conn, make_file):
    conn.execute("insert into rules(description, match_supplier, account_code, status, validated_at) "
                 "values ('CIE -> 605200', 'cie', '605200', 'validated', now())")
    conn.commit()
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), Categorizer(None), today=TODAY)
    assert r.status == "entry_proposed" and r.proposal.source == "rule" and r.proposal.code == "605200"


def test_regle_non_validee_nest_pas_appliquee(conn, make_file):
    conn.execute("insert into rules(description, match_supplier, account_code, status) values ('x', 'cie', '605200', 'proposed')")
    conn.commit()
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), Categorizer(None), today=TODAY)
    assert r.status == "extracted"


def test_correction_du_comptable_propose_une_regle(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser("605100"), today=TODAY)
    out = pipeline.confirm(conn, r.entry_id, account="605200")
    assert out["corrected"] and out["rule_proposed"]
    assert {l["account_code"] for l in lines(conn, r.entry_id)} == {"605200", "445200", "401100"}
    rule = rules.list_rules(conn)[0]
    assert rule["status"] == "proposed" and rule["account_code"] == "605200"
    # elle ne sert que validée
    rules.validate_rule(conn, str(rule["id"])[:8])
    r2 = pipeline.ingest(conn, make_file("b.jpg"), FakeExtractor({**CIE, "date": "2026-10-16"}), Categorizer(None), today=TODAY)
    assert r2.proposal.source == "rule"


def test_deux_confirmations_identiques_proposent_une_regle(conn, make_file):
    for i, jour in enumerate(["2026-09-10", "2026-10-10"]):
        r = pipeline.ingest(conn, make_file(f"f{i}.jpg"), FakeExtractor({**CIE, "date": jour}), chooser(), today=TODAY)
        out = pipeline.confirm(conn, r.entry_id)
    assert out["rule_proposed"]


# ---------------------------------------------------------------- validation et rapports

def test_confirmation_valide_lecriture_et_alimente_le_rapport(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser(), today=TODAY)
    before = reports.daily(conn, date(2026, 10, 15))
    assert "En attente de validation : 1" in before and "Dépenses validées : 0 XOF" in before
    pipeline.confirm(conn, r.entry_id)
    assert conn.execute("select status from entries where id=%s", (r.entry_id,)).fetchone()["status"] == "posted"
    assert conn.execute("select status from documents where id=%s", (r.doc_id,)).fetchone()["status"] == "booked"
    after = reports.daily(conn, date(2026, 10, 15))
    assert "Dépenses validées : 100 000 XOF" in after
    assert "605200" in after
    week = reports.weekly(conn, date(2026, 10, 16))
    assert "100 000 XOF" in week and "2026-10-12" in week


def test_ecriture_validee_est_immuable_et_non_confirmable_deux_fois(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser(), today=TODAY)
    pipeline.confirm(conn, r.entry_id)
    with pytest.raises(ValueError):
        pipeline.confirm(conn, r.entry_id)
    import psycopg
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update entries set label='x' where id=%s", (r.entry_id,))
    conn.rollback()


def test_rejet_supprime_le_brouillon(conn, make_file):
    r = pipeline.ingest(conn, make_file(), FakeExtractor(CIE), chooser(), today=TODAY)
    pipeline.reject(conn, r.entry_id, "pas une dépense de l'entreprise")
    assert conn.execute("select count(*) n from entries").fetchone()["n"] == 0
    assert conn.execute("select status from documents").fetchone()["status"] == "rejected"


def test_echec_de_lecture_conserve_le_fichier_et_pose_une_question(conn, make_file):
    class Boom:
        def extract(self, p):
            raise RuntimeError("API indisponible")
    r = pipeline.ingest(conn, make_file(), Boom(), chooser(), today=TODAY)
    assert r.status == "error"
    d = conn.execute("select status, storage_path from documents").fetchone()
    assert d["status"] == "needs_clarification" and d["storage_path"]


# ---------------------------------------------------------------- dépense par texte (+ étiquettes / entités)

def _cat(code="605300"):
    return Categorizer(lambda accounts, data: {"code": code, "confiance": 0.9, "raison": "t"})


def test_record_expense_texte_auto_poste_avec_etiquettes(conn):
    r = pipeline.record_expense(conn, None, 5000, "carburant", method="especes", emetteur="Total",
                                tags=["Carburant", "Véhicule"], entities=["BYD Song"], categorizer=_cat(), today=TODAY)
    e = conn.execute("select status, tags, entities from entries where id=%s", (r["entry_id"],)).fetchone()
    assert e["status"] == "posted"                                          # auto-posté
    assert conn.execute("select 1 from expense_justifs where entry_id=%s", (r["entry_id"],)).fetchone()  # justif attendu
    assert "carburant" in (e["tags"] or [])                                 # étiquettes (minuscules)
    assert [(l["account_code"], l["debit"], l["credit"]) for l in lines(conn, r["entry_id"])] == [
        ("605300", Decimal("5000"), 0), ("571100", 0, Decimal("5000"))]
    assert conn.execute("select count(*) n from known_tags").fetchone()["n"] >= 2


def test_record_expense_sans_compte_refuse(conn):
    with pytest.raises(ValueError):
        pipeline.record_expense(conn, None, 5000, "truc indéfini", categorizer=Categorizer(None), today=TODAY)


def test_expense_reminder_une_seule_fois(conn):
    r = pipeline.record_expense(conn, None, 5000, "carburant", categorizer=_cat(), today=TODAY)
    conn.execute("update expense_justifs set due_at = now() - interval '1 day' where entry_id=%s", (r["entry_id"],))
    conn.commit()
    acts = pipeline.expense_reminders(conn)
    assert acts and acts[0]["kind"] == "expense_justif"
    assert pipeline.expense_reminders(conn) == []        # relance une seule fois
    assert pipeline.overdue_expense_count(conn) == 1

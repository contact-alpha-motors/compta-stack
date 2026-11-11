"""Module B — sorties d'argent. Deux canaux distincts :

A. SORTIE DE CAISSE (espèces / mobile money)
   demande (tout le monde) -> validation (comptable/admin ou manager, ≠ demandeur)
   -> décaissement (comptable ou manager caissier ; on note QUI décaisse et QUI reçoit)
   -> justificatif sous 24 h (relance du récepteur ET du décaisseur).
   Compta au décaissement : Débit 421 (avance) / Crédit 571 (caisse) ou 521 (mobile money).
   Au rapprochement du justificatif : Débit 6xx (+TVA) / Crédit 421.

B. ORDRE DE VIREMENT (banque)
   seuls manager/admin, AUTOMATIQUE (pas de validation tierce) : crée l'ordre dans l'Assistant
   Caisse + PDF à signer. Sert à approvisionner la caisse. Réception confirmée plus tard (le soir,
   via le rapport) -> Débit 571 (caisse) / Crédit 521 (banque). Pas de justificatif.

Principe : l'IA propose, le CODE vérifie (rôles, séparation), l'humain valide. L'agent ne paie jamais.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import config, pipeline
from .categorize import Categorizer

# admin = comptable. Comptable et manager peuvent valider une sortie de caisse et faire un ordre de virement.
APPROVER_ROLES = ("admin", "accountant", "manager")
VIREMENT_ROLES = ("admin", "accountant", "manager")
CASH_METHODS = ("especes", "mobile_money")


@dataclass
class DisbResult:
    status: str
    request_id: str
    message: str = ""
    flags: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ accès

def _request(conn, request_id: str) -> dict:
    rows = conn.execute("select * from disbursement_requests where id::text like %s", (request_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"Demande introuvable ou ambiguë : {request_id}")
    return rows[0]


def _user(conn, user_id: str) -> dict:
    rows = conn.execute("select * from users where id::text like %s", (user_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"Utilisateur introuvable ou ambigu : {user_id}")
    return rows[0]


def _role_ok(conn, user_id: str, roles: tuple, action: str) -> dict:
    u = _user(conn, user_id)
    if not u["active"]:
        raise ValueError("Utilisateur inactif")
    if u["role"] not in roles:
        raise ValueError(f"Rôle « {u['role']} » non autorisé à {action}")
    return u


def get_request(conn, request_id: str) -> dict:
    return _request(conn, request_id)


def list_requests(conn, status: str | None = None, limit: int = 200) -> list[dict]:
    q = ("select id, requester_id, amount, currency, purpose, beneficiary, payment_method, status, created_at "
         "from disbursement_requests")
    args: tuple = ()
    if status:
        q += " where status = %s"
        args = (status,)
    return conn.execute(q + " order by created_at desc limit %s", (*args, limit)).fetchall()


def documents(conn, request_id: str) -> list[dict]:
    rid = str(_request(conn, request_id)["id"])
    return conn.execute(
        "select id, document_id, kind, created_at from disbursement_documents where request_id = %s order by created_at", (rid,)
    ).fetchall()


def anomalies(conn, req: dict) -> list[str]:
    """Signalé à la validation : le demandeur a-t-il un justificatif en retard ?"""
    flags: list[str] = []
    if req.get("requester_id"):
        late = conn.execute(
            """select count(*) n from disbursement_requests
               where requester_id = %s and status = 'paid'
                 and justif_due_at is not null and justif_due_at < now() and justified_amount is null""",
            (req["requester_id"],)).fetchone()["n"]
        if late:
            flags.append(f"⚠️ le demandeur a {late} justificatif(s) en retard")
    return flags


# ================================================================== A. SORTIE DE CAISSE

def create_request(conn, requester_id: str, amount, purpose: str, beneficiary: str | None = None,
                   method: str = "especes", workspace_id: str | None = None) -> DisbResult:
    """Demande de sortie de caisse (espèces / mobile money). Tout le monde peut demander. Pas de pièce en amont."""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("Le montant doit être strictement positif")
    if not purpose or not purpose.strip():
        raise ValueError("L'objet de la demande est obligatoire")
    if method == "virement":
        raise ValueError("Un virement bancaire se fait par un ordre de virement (direct), pas une sortie de caisse")
    if method not in CASH_METHODS:
        raise ValueError(f"Mode invalide : {method} (especes, mobile_money)")
    req_user = _user(conn, requester_id)
    row = conn.execute(
        """insert into disbursement_requests
             (workspace_id, requester_id, amount, currency, purpose, beneficiary, payment_method, status)
           values (%s,%s,%s,%s,%s,%s,%s,'submitted') returning id""",
        (workspace_id or req_user["workspace_id"], str(req_user["id"]), amount, config.CURRENCY,
         purpose.strip(), beneficiary, method)).fetchone()
    conn.commit()
    return DisbResult("submitted", str(row["id"]),
                      "Demande de sortie de caisse enregistrée. En attente de validation (comptable ou manager).")


def approve(conn, request_id: str, approver_id: str) -> DisbResult:
    req = _request(conn, request_id)
    rid = str(req["id"])
    if req["status"] != "submitted":
        raise ValueError(f"Demande non approuvable (statut : {req['status']})")
    approver = _user(conn, approver_id)
    if not approver["active"]:
        raise ValueError("Utilisateur inactif")
    # La séparation des rôles prime (message clair même si l'approbateur a par ailleurs un rôle valide).
    if req["requester_id"] and str(approver["id"]) == str(req["requester_id"]):
        raise ValueError("Séparation des rôles : le demandeur ne peut pas valider sa propre demande")
    if approver["role"] not in APPROVER_ROLES:
        raise ValueError(f"Rôle « {approver['role']} » non autorisé à valider une sortie de caisse")
    flags = anomalies(conn, req)
    conn.execute("update disbursement_requests set approved_by=%s, approved_at=now(), status='approved' where id=%s",
                 (str(approver["id"]), rid))
    conn.commit()
    return DisbResult("approved", rid, "Validée. Prochaine étape : décaissement (remise du cash).", flags)


def reject(conn, request_id: str, approver_id: str, reason: str = "") -> DisbResult:
    req = _request(conn, request_id)
    rid = str(req["id"])
    if req["status"] not in ("submitted", "approved"):
        raise ValueError(f"Demande non rejetable (statut : {req['status']})")
    _role_ok(conn, approver_id, APPROVER_ROLES, "rejeter une demande")
    conn.execute("update disbursement_requests set status='rejected', rejected_reason=%s where id=%s",
                 (reason or "Rejetée", rid))
    conn.commit()
    return DisbResult("rejected", rid, f"Demande rejetée. Motif : {reason or '—'}")


def _counter_account(method: str) -> tuple[str, str]:
    if method == "especes":
        return config.ACCT_CASH, "CA"
    return config.ACCT_BANK, "BQ"          # mobile money (sous-compte 521)


def _post_advance_entry(conn, req: dict, counter: str, journal: str) -> str:
    label = (f"Avance {req['beneficiary'] or ''} — {req['purpose']}").strip()[:200]
    eid = str(conn.execute(
        """insert into entries(journal_code, entry_date, label, currency, status, proposed_by)
           values (%s, current_date, %s, %s, 'draft', 'agent') returning id""",
        (journal, label, req["currency"])).fetchone()["id"])
    conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                 (eid, config.ACCT_ADVANCE_PERSONNEL, req["amount"], "Avance décaissée"))
    conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,0,%s,%s)",
                 (eid, counter, req["amount"], req["beneficiary"] or "Décaissement"))
    conn.execute("update entries set status='posted' where id=%s", (eid,))
    return eid


def disburse(conn, request_id: str, disburser_id: str) -> DisbResult:
    """Le comptable ou un manager (caissier) remet le cash. On enregistre qui décaisse ; le délai de
    justificatif démarre. Écriture d'avance 421 / trésorerie."""
    req = _request(conn, request_id)
    rid = str(req["id"])
    if req["status"] != "approved":
        raise ValueError(f"Décaissement impossible (statut : {req['status']}) — la demande doit être validée")
    disburser = _role_ok(conn, disburser_id, APPROVER_ROLES, "décaisser")
    counter, journal = _counter_account(req["payment_method"])
    eid = _post_advance_entry(conn, req, counter, journal)
    conn.execute(
        """update disbursement_requests
             set status='paid', disbursed_by=%s, disbursed_at=now(), entry_id=%s, funds_received_at=now(),
                 justif_due_at = now() + make_interval(hours => %s)
           where id=%s""",
        (str(disburser["id"]), eid, config.JUSTIF_DELAY_HOURS, rid))
    conn.commit()
    return DisbResult("paid", rid,
                      f"Décaissé (Débit {config.ACCT_ADVANCE_PERSONNEL} / Crédit {counter}). "
                      f"Justificatif attendu sous {config.JUSTIF_DELAY_HOURS} h (récepteur et décaisseur).")


# ================================================================== B. ORDRE DE VIREMENT (banque)

def _store_pdf(data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    dest = config.INVOICES_DIR / sha[:2] / f"{sha}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(dest)


def create_order(conn, creator_id: str, amount, beneficiary: str, purpose: str, caisse,
                 compte_emetteur: str = "alpha_motors", virement_date: str | None = None,
                 workspace_id: str | None = None) -> DisbResult:
    """Ordre de virement bancaire — AUTOMATIQUE, réservé manager/admin. Crée l'ordre dans l'Assistant
    Caisse et renvoie le PDF à signer. Sert à approvisionner la caisse. Pas de validation tierce, pas de justif."""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("Le montant doit être strictement positif")
    if not beneficiary:
        raise ValueError("Bénéficiaire obligatoire pour un ordre de virement")
    creator = _role_ok(conn, creator_id, VIREMENT_ROLES, "faire un ordre de virement")
    emp = caisse.resolve_employee(beneficiary)        # vérifie l'éligibilité au virement
    resp = caisse.create_virement(emp["id"], amount, motif=purpose, virement_date=virement_date,
                                  compte_emetteur=compte_emetteur)
    pdf_path, pdf_url = None, resp.get("pdf_url")
    if pdf_url:
        try:
            pdf_path = _store_pdf(caisse.download_pdf(pdf_url))
        except Exception:  # noqa: BLE001 — l'URL signée reste utilisable ; ne pas bloquer
            pdf_path = None
    rid = str(conn.execute(
        """insert into disbursement_requests
             (workspace_id, requester_id, amount, currency, purpose, beneficiary, payment_method,
              status, approved_by, approved_at, external_order_ref, order_pdf_path)
           values (%s,%s,%s,%s,%s,%s,'virement','order_created',%s,now(),%s,%s) returning id""",
        (workspace_id or creator["workspace_id"], str(creator["id"]), amount, config.CURRENCY,
         purpose, beneficiary, str(creator["id"]), str(resp.get("id") or ""), pdf_path)).fetchone()["id"])
    conn.commit()
    msg = f"Ordre de virement créé (réf. {resp.get('reference', '?')}). PDF à signer puis envoyer à la banque."
    if not pdf_path and pdf_url:
        msg += f" (PDF : {pdf_url})"
    return DisbResult("order_created", rid, msg)


def pending_receptions(conn) -> list[dict]:
    """Virements dont la réception en caisse n'est pas encore confirmée (à demander au comptable, le soir)."""
    return conn.execute(
        """select id, amount, currency, beneficiary, purpose, created_at from disbursement_requests
           where payment_method='virement' and status='order_created' order by created_at""").fetchall()


def confirm_reception(conn, request_id: str, confirmer_id: str) -> DisbResult:
    """Le comptable confirme que l'argent du virement est bien arrivé en caisse -> Débit 571 / Crédit 521."""
    req = _request(conn, request_id)
    rid = str(req["id"])
    if req["payment_method"] != "virement" or req["status"] != "order_created":
        raise ValueError(f"Réception impossible (statut : {req['status']}, mode : {req['payment_method']})")
    _role_ok(conn, confirmer_id, VIREMENT_ROLES, "confirmer une réception en caisse")
    label = (f"Entrée en caisse (virement) — {req['purpose']}").strip()[:200]
    eid = str(conn.execute(
        """insert into entries(journal_code, entry_date, label, currency, status, proposed_by)
           values ('OD', current_date, %s, %s, 'draft', 'agent') returning id""",
        (label, req["currency"])).fetchone()["id"])
    conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                 (eid, config.ACCT_CASH, req["amount"], "Entrée en caisse"))
    conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,0,%s,%s)",
                 (eid, config.ACCT_BANK, req["amount"], "Virement banque"))
    conn.execute("update entries set status='posted' where id=%s", (eid,))
    conn.execute("update disbursement_requests set status='received', received_at=now(), entry_id=%s where id=%s",
                 (eid, rid))
    conn.commit()
    return DisbResult("received", rid, f"Réception confirmée (Débit {config.ACCT_CASH} / Crédit {config.ACCT_BANK}).")


# ================================================================== Justificatifs : délais, relances, rapprochement

def overdue_requests(conn) -> list[dict]:
    """Sorties de caisse payées dont le justificatif est en retard."""
    return conn.execute(
        """select * from disbursement_requests
           where status = 'paid' and justif_due_at is not null and justif_due_at < now()
             and justified_amount is null order by justif_due_at""").fetchall()


def run_reminders(conn) -> list[dict]:
    """Relance le RÉCEPTEUR et le DÉCAISSEUR des justificatifs en retard ; alerte le comptable après le quota.
    Enregistre les tours et renvoie les actions à envoyer (l'envoi effectif = couche Lark)."""
    actions: list[dict] = []
    for req in overdue_requests(conn):
        rid = str(req["id"])
        agg = conn.execute(
            "select count(*) n, coalesce(bool_or(escalated), false) esc from reminders where request_id=%s",
            (rid,)).fetchone()
        n, escalated = agg["n"], agg["esc"]
        if n < config.JUSTIF_MAX_REMINDERS:
            conn.execute("insert into reminders(request_id, attempt, escalated) values (%s,%s,false)", (rid, n + 1))
            targets = {str(req["requester_id"]) if req["requester_id"] else None,
                       str(req["disbursed_by"]) if req["disbursed_by"] else None}
            for uid in filter(None, targets):
                actions.append({"request_id": rid, "action": "reminder", "attempt": n + 1, "user_id": uid})
        elif not escalated:
            conn.execute("insert into reminders(request_id, attempt, escalated) values (%s,%s,true)", (rid, n + 1))
            actions.append({"request_id": rid, "action": "escalate", "role": "accountant"})
    conn.commit()
    return actions


def reconcile(conn, request_id: str, document_id: str, categorizer: Categorizer) -> DisbResult:
    """Rapproche un justificatif de l'avance : reconnaît la charge contre 421, calcule reliquat/complément."""
    req = _request(conn, request_id)
    rid = str(req["id"])
    if req["status"] != "paid":
        raise ValueError(f"Rapprochement impossible (statut : {req['status']}) — la demande doit être payée")
    docs = conn.execute("select id, extracted from documents where id::text like %s", (document_id + "%",)).fetchall()
    if len(docs) != 1:
        raise ValueError(f"Document introuvable ou ambigu : {document_id}")
    doc = docs[0]
    did = str(doc["id"])
    data = pipeline.normalize(doc["extracted"] or {})
    if data["montant_total"] is None:
        raise ValueError("Justificatif pas encore lu (aucun montant) : passer d'abord par l'ingestion")
    proposal = categorizer.propose(conn, data)
    if proposal is None:
        raise ValueError("Impossible de choisir le compte de charge : catégoriser le justificatif d'abord")
    charge = proposal.code
    justified = data["montant_total"]
    tva = data["tva"] if data["tva"] and data["tva"] > 0 else Decimal(0)
    label = (f"Justificatif {req['beneficiary'] or ''} — {data['libelle'] or req['purpose']}").strip()[:200]
    eid = str(conn.execute(
        """insert into entries(journal_code, entry_date, label, reference, document_id, currency, status, proposed_by, confidence)
           values ('OD', %s, %s, %s, %s, %s, 'draft', 'agent', %s) returning id""",
        (data["date"] or date.today(), label, data["numero_facture"], did, req["currency"], round(proposal.confidence, 2))
    ).fetchone()["id"])
    if tva > 0:
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, charge, justified - tva, "Montant hors taxe"))
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, pipeline._tva_account(charge), tva, "TVA récupérable"))
    else:
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, charge, justified, "Montant TTC"))
    conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,0,%s,%s)",
                 (eid, config.ACCT_ADVANCE_PERSONNEL, justified, "Solde de l'avance"))
    conn.execute("insert into disbursement_documents(request_id, document_id, kind) values (%s,%s,'justificatif')",
                 (rid, did))
    remainder = req["amount"] - justified
    conn.execute(
        "update disbursement_requests set status='justified', justified_amount=%s, remainder=%s, settlement_entry_id=%s where id=%s",
        (justified, remainder, eid, rid))
    conn.execute("update documents set status='booked' where id=%s", (did,))
    conn.commit()
    if remainder > 0:
        tail = f"Reliquat à restituer : {remainder:.0f} {req['currency']}."
    elif remainder < 0:
        tail = f"Complément à rembourser : {-remainder:.0f} {req['currency']}."
    else:
        tail = "Avance soldée exactement."
    return DisbResult("justified", rid, f"Justificatif rapproché (Débit {charge} / Crédit {config.ACCT_ADVANCE_PERSONNEL}). {tail}")


def attach_document(conn, request_id: str, document_id: str, kind: str = "justificatif") -> DisbResult:
    if kind not in ("piece", "justificatif"):
        raise ValueError("kind doit être 'piece' ou 'justificatif'")
    req = _request(conn, request_id)
    rid = str(req["id"])
    doc = conn.execute("select id from documents where id::text like %s", (document_id + "%",)).fetchall()
    if len(doc) != 1:
        raise ValueError(f"Document introuvable ou ambigu : {document_id}")
    conn.execute("insert into disbursement_documents(request_id, document_id, kind) values (%s,%s,%s)",
                 (rid, str(doc[0]["id"]), kind))
    conn.commit()
    return DisbResult(req["status"], rid, "Pièce rattachée.")

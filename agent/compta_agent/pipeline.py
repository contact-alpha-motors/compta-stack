"""Le cœur : document -> lecture -> contrôles -> (question | écriture brouillon) -> confirmation.

Principe : l'IA propose, le code vérifie, le comptable valide.
  * jamais d'écriture validée automatiquement (tout passe par confirm())
  * lecture douteuse => une question, pas une écriture
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from psycopg.types.json import Jsonb

from . import config
from .categorize import Categorizer, Proposal

FCFA_ALIASES = {"XOF", "FCFA", "CFA", "F CFA", "F.CFA", "FRANC CFA"}
PAYMENT_MODES = {"especes", "banque", "mobile_money", "non_paye", "inconnu"}


@dataclass
class Result:
    status: str          # duplicate | needs_clarification | entry_proposed | extracted | error
    doc_id: str | None = None
    entry_id: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    data: dict | None = None
    proposal: Proposal | None = None


# ------------------------------------------------------------------ normalisation

def parse_amount(v) -> Decimal | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = re.sub(r"[^\d,.\-]", "", str(v).replace(" ", "").replace("\xa0", "").replace(" ", ""))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2 or len(parts[1]) == 3:     # 1.250 ou 1.250.000 => milliers (FCFA)
            s = "".join(parts)
        else:
            s = ".".join(parts)
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _text(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def normalize(raw: dict) -> dict:
    """Types propres (date, Decimal...) à partir d'un dictionnaire brut. Idempotent."""
    lis = raw.get("lisibilite")
    try:
        lis = float(lis) if lis is not None else None
    except (TypeError, ValueError):
        lis = None
    mode = (_text(raw, "mode_paiement") or "inconnu").lower()
    devise = _text(raw, "devise")
    return {
        "type_document": _text(raw, "type_document"),
        "emetteur": _text(raw, "emetteur"),
        "date": parse_date(raw.get("date")),
        "libelle": _text(raw, "libelle"),
        "montant_total": parse_amount(raw.get("montant_total")),
        "montant_ht": parse_amount(raw.get("montant_ht")),
        "tva": parse_amount(raw.get("tva")),
        "devise": devise.upper() if devise else None,
        "numero_facture": _text(raw, "numero_facture"),
        "mode_paiement": mode if mode in PAYMENT_MODES else "inconnu",
        "lisibilite": lis,
        "champs_incertains": list(raw.get("champs_incertains") or []),
        "question": _text(raw, "question"),
        "avertissements": list(raw.get("avertissements") or []),
        "corrige_manuellement": list(raw.get("corrige_manuellement") or []),
    }


def jsonable(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = int(v) if v == v.to_integral_value() else float(v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------------ contrôles

def check_problems(data: dict, today: date) -> list[str]:
    """Problèmes BLOQUANTS : on ne crée pas d'écriture, on pose une question."""
    p = []
    if data["lisibilite"] is not None and data["lisibilite"] < config.MIN_LEGIBILITY:
        p.append("document peu lisible")
    if data["date"] is None:
        p.append("date")
    elif data["date"] > today + timedelta(days=1):
        p.append("date dans le futur")
    elif data["date"] < today - timedelta(days=config.MAX_INVOICE_AGE_DAYS):
        p.append("date très ancienne")
    if data["montant_total"] is None:
        p.append("montant")
    elif data["montant_total"] <= 0:
        p.append("montant invalide")
    if not data["libelle"]:
        p.append("libellé")
    dev = data["devise"]
    if dev and dev not in FCFA_ALIASES and dev != "XAF":
        p.append(f"montant en {dev} (la comptabilité est tenue en {config.CURRENCY})")
    if data["tva"] is not None and data["montant_total"] is not None and data["tva"] > data["montant_total"]:
        p.append("TVA supérieure au total")
    return p


def build_question(problems: list[str], model_question: str | None) -> str:
    if model_question:
        return model_question
    return ("Je n'arrive pas à lire ou valider : " + ", ".join(problems) +
            ". Peux-tu renvoyer une photo plus nette, ou me donner ces informations ?")


def soft_warnings(conn, doc_id: str, data: dict) -> list[str]:
    """Points à signaler au comptable sans bloquer."""
    w = []
    if not data["emetteur"]:
        w.append("émetteur non lisible")
    if data["devise"] == "XAF":
        w.append("montant en XAF : traité comme XOF (parité 1:1)")
    if data["champs_incertains"]:
        w.append("champs incertains : " + ", ".join(data["champs_incertains"]))
    if data["emetteur"] and data["montant_total"] is not None and data["date"]:
        dup = conn.execute(
            """select id from documents
               where id <> %s and status in ('entry_proposed', 'booked')
                 and lower(extracted->>'emetteur') = lower(%s)
                 and (extracted->>'montant_total')::numeric = %s
                 and extracted->>'date' = %s limit 1""",
            (doc_id, data["emetteur"], data["montant_total"], data["date"].isoformat()),
        ).fetchone()
        if dup:
            w.append(f"possible doublon du document {str(dup['id'])[:8]} (même émetteur, date et montant)")
    return w


# ------------------------------------------------------------------ stockage

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def store_file(path: Path, sha: str) -> Path:
    dest = config.INVOICES_DIR / sha[:2] / f"{sha}{path.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)
    return dest


# ------------------------------------------------------------------ écriture brouillon

def _tva_account(code: str) -> str:
    if code.startswith("2"):
        return config.ACCT_TVA_IMMO       # TVA récupérable sur immobilisations
    if code.startswith("60"):
        return config.ACCT_TVA_ACHATS     # TVA récupérable sur achats
    return config.ACCT_TVA_SERVICES       # TVA récupérable sur services extérieurs et autres charges


def _payment_side(mode: str) -> tuple[str, str]:
    """(compte de contrepartie, journal) selon le mode de paiement lu."""
    if mode == "especes":
        return config.ACCT_CASH, "CA"
    if mode in ("banque", "mobile_money"):
        return config.ACCT_BANK, "BQ"
    return config.ACCT_SUPPLIER, "AC"     # à payer, ou inconnu : dette fournisseur (le paiement viendra avec l'ordre de virement)


def _third_party(conn, name: str | None) -> str | None:
    if not name:
        return None
    row = conn.execute("select id from third_parties where lower(name) = lower(%s) limit 1", (name,)).fetchone()
    if row:
        return str(row["id"])
    row = conn.execute("insert into third_parties(name, kind) values (%s, 'supplier') returning id", (name,)).fetchone()
    return str(row["id"])


def _insert_lines(conn, entry_id: str, data: dict, code: str, tp_id: str | None) -> None:
    total = data["montant_total"]
    tva = data["tva"] if data["tva"] and data["tva"] > 0 else Decimal(0)
    counter, _ = _payment_side(data["mode_paiement"])
    lines = []
    if tva > 0:
        lines.append((code, total - tva, 0, None, "Montant hors taxe"))
        lines.append((_tva_account(code), tva, 0, None, "TVA récupérable"))
    else:
        lines.append((code, total, 0, None, "Montant TTC (TVA non indiquée)"))
    lines.append((counter, 0, total, tp_id if counter == config.ACCT_SUPPLIER else None, data["emetteur"] or "Fournisseur inconnu"))
    for acc, d, c, tp, memo in lines:
        conn.execute(
            "insert into entry_lines(entry_id, account_code, third_party_id, debit, credit, memo) values (%s,%s,%s,%s,%s,%s)",
            (entry_id, acc, tp, d, c, memo),
        )


def _create_entry(conn, doc_id: str, data: dict, code: str, confidence: float, tp_id: str | None) -> str:
    conn.execute("delete from entries where document_id = %s and status = 'draft'", (doc_id,))
    _, journal = _payment_side(data["mode_paiement"])
    label = f"{data['emetteur'] or 'Fournisseur inconnu'} - {data['libelle']}"[:200]
    row = conn.execute(
        """insert into entries(journal_code, entry_date, label, reference, document_id, currency, status, proposed_by, confidence)
           values (%s,%s,%s,%s,%s,%s,'draft','agent',%s) returning id""",
        (journal, data["date"], label, data["numero_facture"], doc_id, config.CURRENCY, round(confidence, 2)),
    ).fetchone()
    entry_id = str(row["id"])
    _insert_lines(conn, entry_id, data, code, tp_id)
    return entry_id


# ------------------------------------------------------------------ pipeline

def _process(conn, doc_id: str, data: dict, categorizer: Categorizer, today: date | None = None) -> Result:
    today = today or date.today()
    problems = check_problems(data, today)
    data["avertissements"] = soft_warnings(conn, doc_id, data)
    quality = data["lisibilite"]

    if problems:
        question = build_question(problems, data["question"])
        conn.execute(
            "update documents set status='needs_clarification', quality_score=%s, clarification=%s, extracted=%s where id=%s",
            (quality, question, Jsonb(jsonable(data)), doc_id),
        )
        conn.commit()
        return Result("needs_clarification", doc_id=doc_id, message=question, warnings=data["avertissements"], data=data)

    tp_id = _third_party(conn, data["emetteur"])
    proposal = categorizer.propose(conn, data)
    if proposal is None:
        conn.execute(
            "update documents set status='extracted', quality_score=%s, clarification=null, extracted=%s, "
            "invoice_ref=%s, third_party_id=%s where id=%s",
            (quality, Jsonb(jsonable(data)), data["numero_facture"], tp_id, doc_id),
        )
        conn.commit()
        return Result("extracted", doc_id=doc_id, warnings=data["avertissements"], data=data,
                      message="Données lues. Je n'ai pas pu choisir le compte : à catégoriser manuellement.")

    entry_id = _create_entry(conn, doc_id, data, proposal.code, proposal.confidence, tp_id)
    data["proposition"] = {"compte": proposal.code, "source": proposal.source,
                           "confiance": proposal.confidence, "raison": proposal.reason, "regle": proposal.rule_id}
    conn.execute(
        "update documents set status='entry_proposed', quality_score=%s, clarification=null, extracted=%s, "
        "invoice_ref=%s, third_party_id=%s where id=%s",
        (quality, Jsonb(jsonable(data)), data["numero_facture"], tp_id, doc_id),
    )
    conn.commit()
    return Result("entry_proposed", doc_id=doc_id, entry_id=entry_id, warnings=data["avertissements"],
                  data=data, proposal=proposal, message=f"Écriture proposée au compte {proposal.code}")


def ingest(conn, path, extractor, categorizer: Categorizer, today: date | None = None) -> Result:
    path = Path(path)
    sha = sha256_of(path)
    existing = conn.execute("select id, status from documents where sha256 = %s", (sha,)).fetchone()
    if existing:
        return Result("duplicate", doc_id=str(existing["id"]),
                      message=f"Ce fichier a déjà été reçu (document {str(existing['id'])[:8]}, statut : {existing['status']}).")
    stored = store_file(path, sha)
    row = conn.execute(
        "insert into documents(storage_path, sha256, status) values (%s,%s,'received') returning id",
        (str(stored), sha),
    ).fetchone()
    doc_id = str(row["id"])
    conn.commit()   # le fichier est conservé même si la lecture échoue
    try:
        raw = extractor.extract(stored)
    except Exception as e:  # noqa: BLE001
        msg = "Je n'ai pas réussi à lire ce document. Peux-tu le renvoyer (photo nette ou PDF) ?"
        conn.execute("update documents set status='needs_clarification', clarification=%s where id=%s", (msg, doc_id))
        conn.commit()
        return Result("error", doc_id=doc_id, message=f"{msg} [{type(e).__name__}: {e}]")
    return _process(conn, doc_id, normalize(raw), categorizer, today)


def _load_document(conn, doc_id: str) -> tuple[str, dict]:
    row = conn.execute("select id, extracted from documents where id::text like %s", (doc_id + "%",)).fetchall()
    if len(row) != 1:
        raise ValueError(f"Document introuvable ou ambigu : {doc_id}")
    return str(row[0]["id"]), normalize(row[0]["extracted"] or {})


def fix(conn, doc_id: str, updates: dict, categorizer: Categorizer, today: date | None = None) -> Result:
    """Le comptable (ou l'utilisateur) complète les champs manquants, sans relire l'image."""
    doc_id, data = _load_document(conn, doc_id)
    raw = {**jsonable(data), **updates}
    data = normalize(raw)
    data["question"] = None
    data["corrige_manuellement"] = sorted(set(data["corrige_manuellement"]) | set(updates))
    data["lisibilite"] = 1.0
    return _process(conn, doc_id, data, categorizer, today)


def categorize_manual(conn, doc_id: str, code: str) -> Result:
    doc_id, data = _load_document(conn, doc_id)
    acc = conn.execute("select code from accounts where code = %s and status = 'active'", (code,)).fetchone()
    if not acc:
        raise ValueError(f"Compte {code} inconnu ou non validé")
    problems = check_problems(data, date.today())
    if problems:
        raise ValueError("Document incomplet : " + ", ".join(problems))
    tp_id = _third_party(conn, data["emetteur"])
    entry_id = _create_entry(conn, doc_id, data, code, 1.0, tp_id)
    data["proposition"] = {"compte": code, "source": "manual", "confiance": 1.0, "raison": "choix manuel", "regle": None}
    conn.execute("update documents set status='entry_proposed', extracted=%s, third_party_id=%s where id=%s",
                 (Jsonb(jsonable(data)), tp_id, doc_id))
    conn.commit()
    return Result("entry_proposed", doc_id=doc_id, entry_id=entry_id, data=data,
                  proposal=Proposal(code, 1.0, "manual", "choix manuel"), message=f"Écriture proposée au compte {code}")


# ------------------------------------------------------------------ confirmation / rejet

def _entry_by_prefix(conn, entry_id: str) -> dict:
    rows = conn.execute("select * from entries where id::text like %s", (entry_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"Écriture introuvable ou ambiguë : {entry_id}")
    return rows[0]


def _expense_account(conn, entry_id: str) -> str:
    row = conn.execute(
        "select account_code from entry_lines where entry_id = %s and debit > 0 order by debit desc limit 1", (entry_id,)
    ).fetchone()
    return row["account_code"]


def _propose_rule(conn, supplier: str | None, code: str, entry_id: str, why: str) -> str | None:
    if not supplier:
        return None
    exists = conn.execute(
        "select 1 from rules where lower(match_supplier) = lower(%s) and account_code = %s", (supplier, code)
    ).fetchone()
    if exists:
        return None
    row = conn.execute(
        """insert into rules(description, match_supplier, account_code, status, source_entry)
           values (%s,%s,%s,'proposed',%s) returning id""",
        (f"{supplier} -> {code} ({why})", supplier, code, entry_id),
    ).fetchone()
    return str(row["id"])


def confirm(conn, entry_id: str, account: str | None = None) -> dict:
    """Valide une écriture brouillon (avec correction éventuelle du compte). Peut proposer une règle."""
    e = _entry_by_prefix(conn, entry_id)
    if e["status"] != "draft":
        raise ValueError("Cette écriture est déjà validée.")
    eid = str(e["id"])
    doc = conn.execute("select id, extracted, third_party_id from documents where id = %s", (e["document_id"],)).fetchone()
    data = normalize(doc["extracted"] or {})
    current = _expense_account(conn, eid)
    corrected = bool(account and account != current)
    new_rule = None

    if corrected:
        acc = conn.execute("select code from accounts where code = %s and status = 'active'", (account,)).fetchone()
        if not acc:
            raise ValueError(f"Compte {account} inconnu ou non validé")
        conn.execute("delete from entry_lines where entry_id = %s", (eid,))
        _insert_lines(conn, eid, data, account, str(doc["third_party_id"]) if doc["third_party_id"] else None)
        current = account

    conn.execute("update entries set status = 'posted' where id = %s", (eid,))   # les triggers vérifient l'équilibre
    conn.execute("update documents set status = 'booked' where id = %s", (e["document_id"],))

    if corrected:
        new_rule = _propose_rule(conn, data["emetteur"], current, eid, "correction du comptable")
    elif doc["third_party_id"]:
        n = conn.execute(
            """select count(*) as n from entries x join documents d on d.id = x.document_id
               where x.status = 'posted' and d.third_party_id = %s
                 and exists (select 1 from entry_lines l where l.entry_id = x.id and l.account_code = %s)""",
            (doc["third_party_id"], current),
        ).fetchone()["n"]
        if n >= 2:
            new_rule = _propose_rule(conn, data["emetteur"], current, eid, f"{n} confirmations identiques")
    conn.commit()
    return {"entry_id": eid, "account": current, "corrected": corrected, "rule_proposed": new_rule}


def reject(conn, entry_id: str, reason: str = "") -> None:
    e = _entry_by_prefix(conn, entry_id)
    if e["status"] != "draft":
        raise ValueError("Écriture validée : utilise la contre-passation.")
    conn.execute("delete from entries where id = %s", (e["id"],))
    conn.execute("update documents set status='rejected', clarification=%s where id=%s",
                 (reason or "Rejeté par le comptable", e["document_id"]))
    conn.commit()


# ------------------------------------------------------------------ dépense enregistrée par texte (+ étiquettes / entités)

def _expense_counter(method: str) -> tuple[str, str]:
    if method == "especes":
        return config.ACCT_CASH, "CA"
    if method in ("banque", "virement", "mobile_money"):
        return config.ACCT_BANK, "BQ"
    return config.ACCT_SUPPLIER, "AC"          # non payé / inconnu : dette fournisseur


def known_tags(conn, limit: int = 40) -> list[str]:
    """Étiquettes déjà utilisées, à réinjecter dans les prompts pour rester cohérent."""
    return [r["tag"] for r in conn.execute(
        "select tag from known_tags order by uses desc, last_used desc limit %s", (limit,)).fetchall()]


def _register_tags(conn, tags) -> None:
    for t in tags or []:
        conn.execute("""insert into known_tags(tag) values (%s)
                        on conflict (tag) do update set uses = known_tags.uses + 1, last_used = now()""",
                     (t.strip().lower(),))


def record_expense(conn, recorder_id, amount, libelle: str, method: str = "especes", emetteur: str | None = None,
                   tags=None, entities=None, tva=None, categorizer: Categorizer | None = None,
                   today: date | None = None) -> dict:
    """Enregistre une dépense déjà faite (saisie texte) : compte proposé par l'IA, étiquettes, entités.
    Écriture passée automatiquement. Comme il n'y a pas de pièce, un justificatif est attendu sous 24 h."""
    today = today or date.today()
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("Montant invalide")
    if not libelle or not libelle.strip():
        raise ValueError("Objet de la dépense manquant")
    data = {"emetteur": emetteur, "libelle": libelle.strip(), "montant_total": amount,
            "tva": Decimal(str(tva)) if tva else None, "mode_paiement": method, "date": today,
            "devise": config.CURRENCY, "lisibilite": 1.0, "type_document": None, "numero_facture": None,
            "montant_ht": None, "champs_incertains": [], "question": None}
    proposal = categorizer.propose(conn, data) if categorizer else None
    if proposal is None:
        raise ValueError("Impossible de choisir le compte de charge — précise la nature de la dépense")
    code = proposal.code
    counter, journal = _expense_counter(method)
    tp_id = _third_party(conn, emetteur)
    tags = [t.strip().lower() for t in (tags or []) if t and str(t).strip()][:5]
    label = (f"{emetteur or 'Dépense'} — {libelle.strip()}")[:200]
    eid = str(conn.execute(
        """insert into entries(journal_code, entry_date, label, currency, status, proposed_by, confidence,
             tags, entities, recorded_by)
           values (%s,%s,%s,%s,'draft','agent',%s,%s,%s,%s) returning id""",
        (journal, today, label, config.CURRENCY, round(proposal.confidence, 2), tags or None,
         Jsonb(entities) if entities else None, str(recorder_id) if recorder_id else None)).fetchone()["id"])
    tva_val = data["tva"] if data["tva"] and data["tva"] > 0 else Decimal(0)
    if tva_val > 0:
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, code, amount - tva_val, "Montant HT"))
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, _tva_account(code), tva_val, "TVA récupérable"))
    else:
        conn.execute("insert into entry_lines(entry_id, account_code, debit, credit, memo) values (%s,%s,%s,0,%s)",
                     (eid, code, amount, "Montant"))
    conn.execute("insert into entry_lines(entry_id, account_code, third_party_id, debit, credit, memo) values (%s,%s,%s,0,%s,%s)",
                 (eid, counter, tp_id if counter == config.ACCT_SUPPLIER else None, amount, emetteur or "Dépense"))
    conn.execute("update entries set status='posted' where id=%s", (eid,))     # les triggers vérifient l'équilibre
    conn.execute("insert into expense_justifs(entry_id, due_at) values (%s, now() + make_interval(hours => %s))",
                 (eid, config.JUSTIF_DELAY_HOURS))
    _register_tags(conn, tags)
    conn.commit()
    return {"entry_id": eid, "account": code, "counter": counter, "journal": journal, "tags": tags}


def overdue_expense_count(conn) -> int:
    return conn.execute(
        "select count(*) n from expense_justifs where justif_doc_id is null and due_at < now()").fetchone()["n"]


def expense_reminders(conn) -> list[dict]:
    """Relance (une fois) l'auteur d'une dépense-texte dont le justificatif manque passé l'échéance."""
    rows = conn.execute(
        """select j.entry_id, e.label, e.recorded_by from expense_justifs j join entries e on e.id = j.entry_id
           where j.justif_doc_id is null and j.due_at < now() and j.reminded_at is null""").fetchall()
    actions = []
    for r in rows:
        conn.execute("update expense_justifs set reminded_at = now() where entry_id = %s", (r["entry_id"],))
        actions.append({"entry_id": str(r["entry_id"]), "label": r["label"], "kind": "expense_justif",
                        "user_id": str(r["recorded_by"]) if r["recorded_by"] else None})
    conn.commit()
    return actions

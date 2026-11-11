"""Rapports journalier / hebdomadaire (texte, prêts à être envoyés dans Lark)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import config


def fmt(x) -> str:
    x = Decimal(x or 0)
    return f"{x:,.0f}".replace(",", " ") + f" {config.CURRENCY}"


def _period(conn, start: date, end: date) -> dict:
    """Totaux des écritures VALIDÉES entre start et end (inclus) + brouillons en attente."""
    tot = conn.execute(
        """select coalesce(sum(l.debit - l.credit) filter (where a.class = 6), 0) as depenses,
                  coalesce(sum(l.credit - l.debit) filter (where a.class = 7), 0) as recettes,
                  count(distinct e.id) as nb
           from entries e join entry_lines l on l.entry_id = e.id join accounts a on a.code = l.account_code
           where e.status = 'posted' and e.entry_date between %s and %s""", (start, end)).fetchone()
    by_acc = conn.execute(
        """select a.code, a.label, sum(l.debit - l.credit) as solde
           from entries e join entry_lines l on l.entry_id = e.id join accounts a on a.code = l.account_code
           where e.status = 'posted' and a.class = 6 and e.entry_date between %s and %s
           group by a.code, a.label having sum(l.debit - l.credit) <> 0 order by solde desc""", (start, end)).fetchall()
    pending = conn.execute(
        """select count(*) as nb, coalesce(sum(t.total), 0) as total from entries e
           join (select entry_id, sum(credit) as total from entry_lines group by entry_id) t on t.entry_id = e.id
           where e.status = 'draft' and e.entry_date between %s and %s""", (start, end)).fetchone()
    clar = conn.execute("select count(*) as nb from documents where status = 'needs_clarification'").fetchone()
    # Justificatifs de décaissement en retard (instantané, indépendant de la période).
    late = conn.execute(
        """select count(*) as nb, coalesce(sum(amount), 0) as total from disbursement_requests
           where status in ('paid', 'order_created') and justif_due_at is not null
             and justif_due_at < now() and justified_amount is null""").fetchone()
    exp_late = conn.execute(
        "select count(*) as nb from expense_justifs where justif_doc_id is null and due_at < now()").fetchone()["nb"]
    reception = conn.execute(
        "select count(*) as nb from disbursement_requests where payment_method='virement' and status='order_created'").fetchone()["nb"]
    return {"tot": tot, "by_acc": by_acc, "pending": pending, "clar": clar["nb"],
            "late": late, "exp_late": exp_late, "reception": reception}


def render(title: str, p: dict) -> str:
    lines = [title,
             f"Dépenses validées : {fmt(p['tot']['depenses'])} ({p['tot']['nb']} écriture(s))",
             f"Recettes validées : {fmt(p['tot']['recettes'])}"]
    if p["by_acc"]:
        lines.append("Dépenses par compte :")
        lines += [f"  {r['code']} {r['label']} : {fmt(r['solde'])}" for r in p["by_acc"]]
    if p["pending"]["nb"]:
        lines.append(f"En attente de validation : {p['pending']['nb']} écriture(s), {fmt(p['pending']['total'])}")
    if p["clar"]:
        lines.append(f"Documents en attente de précision : {p['clar']}")
    if p["late"]["nb"]:
        lines.append(f"Justificatifs de décaissement en retard : {p['late']['nb']} ({fmt(p['late']['total'])} avancés)")
    if p.get("exp_late"):
        lines.append(f"Dépenses sans justificatif en retard : {p['exp_late']}")
    if p.get("reception"):
        lines.append(f"Virements en attente de réception en caisse : {p['reception']} — confirmer si l'argent est arrivé")
    return "\n".join(lines)


def daily(conn, day: date | None = None) -> str:
    day = day or date.today()
    return render(f"Rapport du {day.isoformat()}", _period(conn, day, day))


def weekly(conn, day: date | None = None) -> str:
    day = day or date.today()
    start = day - timedelta(days=day.weekday())          # lundi
    end = start + timedelta(days=6)
    return render(f"Rapport de la semaine du {start.isoformat()} au {end.isoformat()}", _period(conn, start, end))

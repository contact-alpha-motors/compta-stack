"""Interface en ligne de commande (sert de banc d'essai avant Lark).

  ingest  <fichier...>            lit des factures (jpg/png/pdf) et propose des écritures
  review                          liste ce qui attend le comptable
  confirm <écriture> [--compte X] valide une écriture (avec correction éventuelle du compte)
  reject  <écriture> [--motif ..] rejette un brouillon
  fix     <document> --date .. --montant .. --libelle .. --emetteur ..   complète une lecture
  categorize <document> <compte>  impute manuellement un document déjà lu
  rules [--statut proposed]       liste les règles ; rules validate <id> ; rules disable <id>
  report  daily|weekly [--date AAAA-MM-JJ]
  accounts [list] [--statut X] [--prefixe 60]   plan comptable (Odoo) ; accounts activate <code> ;
           accounts disable <code> ; accounts import-mapping <feuille.csv>
  request new --montant X --objet "..." --demandeur <id> [--beneficiaire ..] [--methode especes|mobile_money]  (sortie de caisse)
  request approve|reject <demande> --par <id> [--motif ..]
  request disburse <demande> --par <id>     remet le cash (écriture d'avance 421)
  request order --montant X --beneficiaire Y --par <id> [--objet ..] [--compte ..]   ordre de virement banque (direct) + PDF
  request receive <demande> --par <id>      confirme la réception en caisse d'un virement (Débit 571 / Crédit 521)
  request reconcile <demande> <document>    rapproche un justificatif
  request show <demande> ; request list [--statut]
  reminders                       relance les justificatifs en retard (puis alerte comptable)
  users add --openid <id> --nom "..." [--role requester|accountant|manager|admin] [--espace NOM]
  lark serve|report|remind        bot Lark (connexion longue) : écoute / envoie le rapport / relance
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import accounts, assistant_caisse, config, db, disbursements, pipeline, reports, rules
from .categorize import Categorizer, make_llm_chooser
from .extract import LlmExtractor


def _short(x) -> str:
    return str(x)[:8]


def cmd_ingest(conn, a):
    extractor = LlmExtractor()
    categorizer = Categorizer(make_llm_chooser())
    for f in a.files:
        r = pipeline.ingest(conn, Path(f), extractor, categorizer)
        print(f"[{Path(f).name}] {r.status} - doc {_short(r.doc_id)}" + (f" - écriture {_short(r.entry_id)}" if r.entry_id else ""))
        if r.message:
            print("   ", r.message)
        if r.data and r.status in ("entry_proposed", "extracted"):
            d = r.data
            print(f"    {d['date']} | {d['emetteur'] or '?'} | {d['libelle']} | {d['montant_total']} {d['devise'] or config.CURRENCY}")
        if r.proposal:
            print(f"    compte {r.proposal.code} ({r.proposal.source}, confiance {r.proposal.confidence:.2f}) {r.proposal.reason}")
        for w in r.warnings:
            print("    ! " + w)


def cmd_review(conn, a):
    docs = conn.execute(
        "select id, clarification from documents where status = 'needs_clarification' order by created_at").fetchall()
    drafts = conn.execute(
        """select e.id, e.entry_date, e.label, e.confidence,
                  (select sum(credit) from entry_lines where entry_id = e.id) as total,
                  (select account_code from entry_lines where entry_id = e.id and debit > 0 order by debit desc limit 1) as compte
           from entries e where e.status = 'draft' order by e.confidence nulls first, e.entry_date""").fetchall()
    print(f"== Documents à préciser : {len(docs)}")
    for d in docs:
        print(f"  {_short(d['id'])}  {d['clarification']}")
    print(f"== Écritures à valider : {len(drafts)} (les moins sûres d'abord)")
    for e in drafts:
        print(f"  {_short(e['id'])}  {e['entry_date']}  {reports.fmt(e['total']):>16}  compte {e['compte']}  conf {e['confidence']}  {e['label']}")


def cmd_confirm(conn, a):
    r = pipeline.confirm(conn, a.entry, a.compte)
    print(f"Écriture {_short(r['entry_id'])} validée au compte {r['account']}" + (" (corrigé)" if r["corrected"] else ""))
    if r["rule_proposed"]:
        print(f"Règle proposée {_short(r['rule_proposed'])} : à valider avec `rules validate {_short(r['rule_proposed'])}`")


def cmd_reject(conn, a):
    pipeline.reject(conn, a.entry, a.motif or "")
    print("Écriture rejetée.")


def cmd_fix(conn, a):
    updates = {k: v for k, v in {"date": a.date, "montant_total": a.montant, "libelle": a.libelle,
                                 "emetteur": a.emetteur, "tva": a.tva}.items() if v is not None}
    if not updates:
        sys.exit("Rien à corriger : donne au moins --date, --montant, --libelle, --emetteur ou --tva")
    r = pipeline.fix(conn, a.document, updates, Categorizer(make_llm_chooser()))
    print(r.status, "-", r.message)


def cmd_categorize(conn, a):
    r = pipeline.categorize_manual(conn, a.document, a.compte)
    print(f"Écriture {_short(r.entry_id)} proposée au compte {a.compte}")


def cmd_rules(conn, a):
    if a.action == "validate":
        rules.validate_rule(conn, a.id)
        print("Règle validée.")
    elif a.action == "disable":
        rules.disable_rule(conn, a.id)
        print("Règle désactivée.")
    else:
        for r in rules.list_rules(conn, a.statut):
            print(f"  {_short(r['id'])}  [{r['status']}]  {r['description']}")


def cmd_report(conn, a):
    day = date.fromisoformat(a.date) if a.date else None
    print(reports.daily(conn, day) if a.kind == "daily" else reports.weekly(conn, day))


def cmd_request(conn, a):
    if a.action == "new":
        if a.montant is None or not a.objet:
            sys.exit("Requis : --montant et --objet (+ --demandeur <id>)")
        if not a.demandeur:
            sys.exit("Requis : --demandeur <id utilisateur>")
        r = disbursements.create_request(conn, a.demandeur, a.montant, a.objet, a.beneficiaire,
                                         method=a.methode or "especes")
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "attach":
        if not a.ref or not a.doc:
            sys.exit("Requis : request attach <demande> <document> [--type piece|justificatif]")
        r = disbursements.attach_document(conn, a.ref, a.doc, a.type or "piece")
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "approve":
        if not a.ref or not a.par:
            sys.exit("Requis : request approve <demande> --par <approbateur>")
        r = disbursements.approve(conn, a.ref, a.par)
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
        for f in r.flags:
            print("    ! " + f)
    elif a.action == "reject":
        if not a.ref or not a.par:
            sys.exit("Requis : request reject <demande> --par <approbateur> [--motif ..]")
        r = disbursements.reject(conn, a.ref, a.par, a.motif or "")
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "order":
        if not (a.montant and a.beneficiaire and a.par):
            sys.exit("Requis : request order --montant X --beneficiaire Y --par <id> [--objet ..] [--compte ..]")
        caisse = assistant_caisse.AssistantCaisse()
        r = disbursements.create_order(conn, a.par, a.montant, a.beneficiaire, a.objet or "Approvisionnement caisse",
                                       caisse, compte_emetteur=a.compte or "alpha_motors")
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action in ("disburse", "pay"):
        if not a.ref or not a.par:
            sys.exit("Requis : request disburse <demande> --par <décaisseur>")
        r = disbursements.disburse(conn, a.ref, a.par)
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "receive":
        if not a.ref or not a.par:
            sys.exit("Requis : request receive <demande> --par <comptable>")
        r = disbursements.confirm_reception(conn, a.ref, a.par)
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "reconcile":
        if not a.ref or not a.doc:
            sys.exit("Requis : request reconcile <demande> <document justificatif>")
        r = disbursements.reconcile(conn, a.ref, a.doc, Categorizer(make_llm_chooser()))
        print(f"Demande {_short(r.request_id)} [{r.status}] — {r.message}")
    elif a.action == "show":
        req = disbursements.get_request(conn, a.ref)
        print(f"Demande {_short(req['id'])} [{req['status']}]  {reports.fmt(req['amount'])}  {req['purpose']}"
              + (f"  bénéf. {req['beneficiary']}" if req['beneficiary'] else ""))
        for doc in disbursements.documents(conn, a.ref):
            print(f"    pièce {_short(doc['document_id'])} ({doc['kind']})")
        for f in disbursements.anomalies(conn, req):
            print("    ! " + f)
    else:  # list
        for req in disbursements.list_requests(conn, a.statut):
            print(f"  {_short(req['id'])}  [{req['status']:<11}] {reports.fmt(req['amount']):>16}  {req['purpose']}")


def cmd_users(conn, a):
    if a.action != "add" or not (a.openid and a.nom):
        sys.exit('Usage : users add --openid <id Lark> --nom "..." [--role requester|accountant|manager|admin] [--espace NOM]')
    ws_name = a.espace or "Alpha Motors"
    ws = conn.execute("select id from workspaces where name=%s and channel='lark'", (ws_name,)).fetchone()
    if not ws:
        ws = conn.execute("insert into workspaces(name, channel) values (%s,'lark') returning id", (ws_name,)).fetchone()
    conn.execute(
        """insert into users(workspace_id, external_id, full_name, role) values (%s,%s,%s,%s)
           on conflict (workspace_id, external_id)
           do update set full_name=excluded.full_name, role=excluded.role, active=true""",
        (ws["id"], a.openid, a.nom, a.role or "requester"))
    conn.commit()
    print(f"Utilisateur {a.nom} ({a.role or 'requester'}) autorisé dans l'espace « {ws_name} ».")


def cmd_lark(conn, a):
    from . import lark_bot
    bot = lark_bot.LarkBot()
    if a.action == "report":
        if not config.LARK_REPORT_CHAT_ID:
            sys.exit("LARK_REPORT_CHAT_ID non défini")
        bot.send_daily_report(config.LARK_REPORT_CHAT_ID)
        print("Rapport envoyé.")
    elif a.action == "remind":
        n = bot.notify_reminders()
        print(f"{n} message(s) de relance envoyé(s).")
    else:  # serve
        pairs = [(config.LARK_APP_ID, config.LARK_APP_SECRET), (config.LARK_APP_ID_2, config.LARK_APP_SECRET_2)]
        apps = [(i, s) for i, s in pairs if i and s]
        if not apps:
            sys.exit("Aucune application Lark configurée (LARK_APP_ID / LARK_APP_SECRET)")
        print(f"Bot Lark en écoute (connexion longue) sur {len(apps)} espace(s). Ctrl+C pour arrêter.")
        lark_bot.serve_all(apps)


def cmd_reminders(conn, a):
    actions = disbursements.run_reminders(conn)
    if not actions:
        print("Aucune relance à envoyer.")
    for act in actions:
        who = "demandeur" if act["action"] == "reminder" else "COMPTABLE (alerte)"
        print(f"  {act['action']} -> {who}  demande {_short(act['request_id'])}" +
              (f"  (relance {act['attempt']})" if act.get('attempt') else ""))


def cmd_accounts(conn, a):
    if a.action == "activate":
        accounts.activate(conn, a.code)
        print(f"Compte {a.code} activé.")
    elif a.action == "disable":
        accounts.disable(conn, a.code)
        print(f"Compte {a.code} désactivé.")
    elif a.action == "import-mapping":
        n = accounts.import_mapping(conn, a.code)      # a.code porte le chemin du CSV ici
        print(f"{n} compte(s) activé(s) depuis la feuille de correspondance.")
    else:
        for r in accounts.list_accounts(conn, a.statut, a.prefixe):
            print(f"  {r['code']:<8} [{r['status']:<8}] {r['label']}" + (f"  — {r['usage_hint']}" if r['usage_hint'] else ""))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="compta_agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest"); s.add_argument("files", nargs="+"); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("review"); s.set_defaults(fn=cmd_review)
    s = sub.add_parser("confirm"); s.add_argument("entry"); s.add_argument("--compte"); s.set_defaults(fn=cmd_confirm)
    s = sub.add_parser("reject"); s.add_argument("entry"); s.add_argument("--motif"); s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("fix"); s.add_argument("document")
    for k in ("date", "montant", "libelle", "emetteur", "tva"):
        s.add_argument(f"--{k}")
    s.set_defaults(fn=cmd_fix)
    s = sub.add_parser("categorize"); s.add_argument("document"); s.add_argument("compte"); s.set_defaults(fn=cmd_categorize)
    s = sub.add_parser("rules"); s.add_argument("action", nargs="?", choices=["list", "validate", "disable"], default="list")
    s.add_argument("id", nargs="?"); s.add_argument("--statut"); s.set_defaults(fn=cmd_rules)
    s = sub.add_parser("report"); s.add_argument("kind", choices=["daily", "weekly"]); s.add_argument("--date"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("accounts"); s.add_argument("action", nargs="?", choices=["list", "activate", "disable", "import-mapping"], default="list")
    s.add_argument("code", nargs="?"); s.add_argument("--statut"); s.add_argument("--prefixe"); s.set_defaults(fn=cmd_accounts)
    s = sub.add_parser("request"); s.add_argument("action", nargs="?",
        choices=["new", "attach", "approve", "reject", "order", "disburse", "pay", "receive", "reconcile", "show", "list"], default="list")
    s.add_argument("ref", nargs="?"); s.add_argument("doc", nargs="?")
    for k in ("montant", "objet", "beneficiaire", "demandeur", "date", "methode", "par", "motif", "type", "compte", "statut"):
        s.add_argument(f"--{k}")
    s.set_defaults(fn=cmd_request)
    s = sub.add_parser("reminders"); s.set_defaults(fn=cmd_reminders)
    s = sub.add_parser("users"); s.add_argument("action", nargs="?", choices=["add"], default="add")
    for k in ("openid", "nom", "role", "espace"):
        s.add_argument(f"--{k}")
    s.set_defaults(fn=cmd_users)
    s = sub.add_parser("lark"); s.add_argument("action", nargs="?", choices=["serve", "report", "remind"], default="serve")
    s.set_defaults(fn=cmd_lark)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    conn = db.connect(actor="cli")
    try:
        a.fn(conn, a)
    except ValueError as e:
        sys.exit(f"Erreur : {e}")
    finally:
        conn.close()

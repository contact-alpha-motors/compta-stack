"""Choix du compte de charge (ou d'immobilisation) pour un document lu.

Ordre de décision :
  1. une règle VALIDÉE par le comptable (fournisseur / mots-clés) -> confiance haute, source "rule"
  2. sinon Claude choisit parmi les comptes ACTIFS, en s'aidant des indices d'usage -> source "llm"
Le résultat n'est jamais définitif : il devient une écriture brouillon que le comptable confirme.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import config, llm


@dataclass
class Proposal:
    code: str
    confidence: float
    source: str            # rule | llm | manual
    reason: str = ""
    rule_id: str | None = None


def candidate_accounts(conn) -> list[dict]:
    """Comptes sur lesquels une facture peut être imputée : charges (hors dotations) et immobilisations."""
    return conn.execute(
        """select code, label, coalesce(usage_hint, '') as usage_hint
           from accounts
           where status = 'active'
             and ((code like '6%%' and code not like '68%%' and code not like '69%%')
               or (code like '2%%' and code not like '28%%'))
           order by code"""
    ).fetchall()


def _by_rule(conn, data: dict) -> Proposal | None:
    supplier = data.get("emetteur") or ""
    libelle = data.get("libelle") or ""
    row = conn.execute(
        """select r.id, r.account_code, r.description
           from rules r join accounts a on a.code = r.account_code and a.status = 'active'
           where r.status = 'validated'
             and (
               (r.match_supplier is not null and r.match_supplier <> ''
                  and %s ilike '%%' || r.match_supplier || '%%')
               or (r.match_keywords is not null and cardinality(r.match_keywords) > 0
                  and %s ilike all (select '%%' || k || '%%' from unnest(r.match_keywords) as k))
             )
           order by r.validated_at desc nulls last
           limit 1""",
        (supplier, libelle),
    ).fetchone()
    if not row:
        return None
    return Proposal(row["account_code"], 0.95, "rule", f"Règle validée : {row['description']}", str(row["id"]))


class Categorizer:
    def __init__(self, choose_fn: Callable[[list[dict], dict], dict] | None = None):
        self.choose_fn = choose_fn

    def propose(self, conn, data: dict) -> Proposal | None:
        p = _by_rule(conn, data)
        if p:
            return p
        if self.choose_fn is None:
            return None
        accounts = candidate_accounts(conn)
        allowed = {a["code"] for a in accounts}
        choice = self.choose_fn(accounts, data)
        code = (choice or {}).get("code")
        if not code or code not in allowed:
            return None
        return Proposal(code, float(choice.get("confiance") or 0), "llm", choice.get("raison") or "")


# ---------------------------------------------------------------- modèle (OpenRouter)

CHOOSE_TOOL = {
    "name": "choisir_compte",
    "description": "Choisit le compte SYSCOHADA d'imputation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": ["string", "null"], "description": "Code du compte choisi dans la liste, ou null si aucun ne convient."},
            "confiance": {"type": "number", "description": "0 à 1."},
            "raison": {"type": "string", "description": "Une phrase courte en français."},
        },
        "required": ["code", "confiance", "raison"],
    },
}

CHOOSE_SYSTEM = """Tu es comptable (SYSCOHADA). On te donne un document (émetteur, libellé, montant) et la liste des
comptes autorisés avec une indication d'usage. Choisis le compte d'imputation le plus précis qui convient.
- Un achat durable de valeur importante (véhicule, ordinateur, machine) va en classe 2 ; une dépense courante en classe 6.
- Prends le sous-compte le plus précis de la liste quand il correspond (ex. 6053 plutôt que 605).
- Si aucun compte de la liste ne convient vraiment, renvoie code = null.
- Sois honnête sur ta confiance : un doute réel => confiance basse.
Réponds uniquement via l'outil."""


def make_llm_chooser(client=None, model: str | None = None) -> Callable[[list[dict], dict], dict]:
    model = model or config.CATEGORIZE_MODEL
    state = {"client": client}                      # création paresseuse : pas de clé requise tant qu'on ne catégorise rien

    def choose(accounts: list[dict], data: dict) -> dict:
        if state["client"] is None:
            state["client"] = llm.make_client()
        listing = "\n".join(f"{a['code']} | {a['label']} | {a['usage_hint']}" for a in accounts)
        doc = (f"Émetteur : {data.get('emetteur') or 'inconnu'}\n"
               f"Libellé : {data.get('libelle')}\n"
               f"Montant TTC : {data.get('montant_total')} {data.get('devise') or config.CURRENCY}\n"
               f"Type : {data.get('type_document') or 'inconnu'}")
        content = f"Document :\n{doc}\n\nComptes autorisés (code | libellé | usage) :\n{listing}"
        return llm.call_tool(state["client"], model, CHOOSE_SYSTEM, content, CHOOSE_TOOL, max_tokens=400)

    return choose

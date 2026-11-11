"""Cartes interactives Lark (boutons). Construites ici, envoyées par lark_bot.

Pur / testable : ces fonctions ne renvoient que des dictionnaires (le JSON de la carte)."""
from __future__ import annotations


def approval_card(request_id: str, amount_str: str, purpose: str, requester_name: str) -> dict:
    """Carte de validation d'une sortie de caisse, avec boutons Approuver / Refuser."""
    rid = str(request_id)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🧾 Sortie de caisse à valider"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"**Montant :** {amount_str}\n**Objet :** {purpose}\n"
                f"**Demandeur :** {requester_name}\n**Réf :** {rid[:8]}"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✅ Approuver"},
                 "type": "primary", "value": {"a": "approve", "rid": rid}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "❌ Refuser"},
                 "type": "danger", "value": {"a": "reject", "rid": rid}},
            ]},
        ],
    }


def result_card(text: str, ok: bool = True) -> dict:
    """Carte simple (sans bouton) qui remplace la carte de validation une fois la décision prise."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green" if ok else "red",
                   "title": {"tag": "plain_text", "content": "Décision enregistrée" if ok else "Action"}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
    }

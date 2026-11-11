"""Gestion des utilisateurs du bot (liste blanche) : rôles + message de bienvenue."""
from __future__ import annotations

ROLE_SYNONYMS = {
    "admin": "admin", "administrateur": "admin", "comptable": "admin", "compta": "admin",
    "manager": "manager", "caissier": "manager", "caissière": "manager", "caisse": "manager",
    "requester": "requester", "demandeur": "requester", "employe": "requester", "employé": "requester",
    "utilisateur": "requester",
}

ROLE_LABEL = {"admin": "administrateur (comptable)", "manager": "manager (caissier)", "requester": "utilisateur"}


def normalize_role(text: str | None) -> str | None:
    if not text:
        return None
    return ROLE_SYNONYMS.get(text.strip().lower())


def upsert_user(conn, workspace_id, open_id: str, full_name: str, role: str) -> None:
    """Inscrit / met à jour un utilisateur en liste blanche (par espace + open_id)."""
    conn.execute(
        """insert into users(workspace_id, external_id, full_name, role)
           values (%s,%s,%s,%s)
           on conflict (workspace_id, external_id)
           do update set full_name = excluded.full_name, role = excluded.role, active = true""",
        (workspace_id, open_id, full_name or open_id, role))
    conn.commit()


def welcome_text(role: str, name: str) -> str:
    base = f"👋 Bonjour {name}. Vous êtes désormais **{ROLE_LABEL.get(role, role)}** de l'assistant comptable.\n"
    if role == "admin":
        actions = ("Vous pouvez : valider / rejeter les sorties de caisse, décaisser le cash, créer des ordres de "
                   "virement bancaires, enregistrer des dépenses et consulter les rapports.")
    elif role == "manager":
        actions = ("Vous pouvez : valider / rejeter les sorties de caisse, décaisser le cash, créer des ordres de "
                   "virement bancaires, et enregistrer des dépenses.")
    else:
        actions = "Vous pouvez : demander une sortie de caisse et enregistrer des dépenses."
    return (base + actions +
            "\nParlez-moi simplement en langage naturel (ex. « décaisse 20 000 pour le taxi », "
            "« montre les dépenses du jour »). Tapez « aide » à tout moment.")

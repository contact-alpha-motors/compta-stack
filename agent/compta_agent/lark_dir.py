"""Annuaire Lark : énumération récursive des employés d'un espace + résolution par nom + envoi de message.

Basé sur l'API contact v3 (lecture seule) : on parcourt TOUT l'arbre des départements autorisés
(find_by_department ne renvoie que les users directs d'un département, pas les sous-départements).
httpx est importé tardivement (requis seulement à l'exécution réelle, pas pour les tests mockés).
"""
from __future__ import annotations

from . import config


def _client():
    import httpx
    return httpx


def _token(http, app_id: str, app_secret: str, domain: str) -> str | None:
    r = http.post(f"{domain}/open-apis/auth/v3/tenant_access_token/internal",
                  json={"app_id": app_id, "app_secret": app_secret}, timeout=20).json()
    return r.get("tenant_access_token")


def list_employees(app_id: str, app_secret: str, domain: str | None = None) -> list[dict]:
    """Tous les employés visibles par l'app : [{open_id, name}], dédupliqués, arbre parcouru récursivement."""
    http = _client()
    domain = domain or config.LARK_DOMAIN
    tat = _token(http, app_id, app_secret, domain)
    if not tat:
        return []
    headers = {"Authorization": f"Bearer {tat}"}

    def g(path, params):
        return http.get(domain + path, headers=headers, params=params, timeout=25).json()

    scopes = (g("/open-apis/contact/v3/scopes",
                {"department_id_type": "open_department_id", "user_id_type": "open_id", "page_size": 50}).get("data", {}) or {})
    roots = list(scopes.get("department_ids", []) or [])
    direct = list(scopes.get("user_ids", []) or [])

    depts = set(roots)
    for root in roots:
        tok = None
        while True:
            p = {"parent_department_id": root, "department_id_type": "open_department_id", "fetch_child": True, "page_size": 50}
            if tok:
                p["page_token"] = tok
            d = g("/open-apis/contact/v3/departments", p).get("data", {}) or {}
            for dep in (d.get("items", []) or []):
                if dep.get("open_department_id"):
                    depts.add(dep["open_department_id"])
            tok = d.get("page_token")
            if not d.get("has_more"):
                break

    people: dict[str, str | None] = {oid: None for oid in direct}
    for dep in list(depts) + ["0"]:
        dtype = "department_id" if dep == "0" else "open_department_id"
        tok = None
        while True:
            p = {"department_id": dep, "department_id_type": dtype, "user_id_type": "open_id", "page_size": 50}
            if tok:
                p["page_token"] = tok
            d = g("/open-apis/contact/v3/users/find_by_department", p).get("data", {}) or {}
            for e in (d.get("items", []) or []):
                people[e.get("open_id")] = e.get("name") or people.get(e.get("open_id"))
            tok = d.get("page_token")
            if not d.get("has_more"):
                break
    return [{"open_id": k, "name": v} for k, v in people.items() if k]


def resolve(name: str, employees: list[dict]) -> dict | None:
    """Trouve un employé par nom (exact puis partiel). None si introuvable ou ambigu."""
    if not name:
        return None
    needle = name.strip().lower()
    exact = [e for e in employees if e["name"] and e["name"].strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [e for e in employees if e["name"] and needle in e["name"].strip().lower()]
    return partial[0] if len(partial) == 1 else None


def send_text(app_id: str, app_secret: str, domain: str, open_id: str, text: str) -> bool:
    import json
    http = _client()
    tat = _token(http, app_id, app_secret, domain)
    if not tat:
        return False
    r = http.post(f"{domain}/open-apis/im/v1/messages",
                  headers={"Authorization": f"Bearer {tat}", "Content-Type": "application/json"},
                  params={"receive_id_type": "open_id"},
                  json={"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text})},
                  timeout=20).json()
    return r.get("code") == 0

"""Plan comptable : consultation et activation de comptes par le comptable.

Le plan Odoo complet est importé en 'pending' (03-plan-comptable.sql). Un sous-ensemble est
activé d'office (04). Le comptable active le reste — à l'unité, ou en masse depuis la feuille
initdb/plan-comptable-mapping.csv une fois la colonne `odoo_propose` renseignée pour les comptes
« à confirmer ». L'agent n'impute jamais sur un compte non 'active' (garde-fou).
"""
from __future__ import annotations

import csv


def list_accounts(conn, status: str | None = None, prefix: str | None = None, limit: int = 200) -> list[dict]:
    q = "select code, label, status, coalesce(usage_hint, '') as usage_hint from accounts"
    cond: list[str] = []
    args: list = []
    if status:
        cond.append("status = %s")
        args.append(status)
    if prefix:
        cond.append("code like %s")
        args.append(prefix + "%")
    if cond:
        q += " where " + " and ".join(cond)
    q += " order by code limit %s"
    args.append(limit)
    return conn.execute(q, tuple(args)).fetchall()


def _exists(conn, code: str) -> bool:
    return conn.execute("select 1 from accounts where code = %s", (code,)).fetchone() is not None


def activate(conn, code: str, label: str | None = None, usage_hint: str | None = None) -> None:
    if not _exists(conn, code):
        raise ValueError(f"Compte {code} absent du plan comptable")
    sets = ["status = 'active'", "validated_at = now()"]
    args: list = []
    if label:
        sets.append("label = %s")
        args.append(label)
    if usage_hint is not None:
        sets.append("usage_hint = %s")
        args.append(usage_hint)
    args.append(code)
    conn.execute(f"update accounts set {', '.join(sets)} where code = %s", tuple(args))
    conn.commit()


def disable(conn, code: str) -> None:
    if not _exists(conn, code):
        raise ValueError(f"Compte {code} absent du plan comptable")
    conn.execute("update accounts set status = 'rejected' where code = %s", (code,))
    conn.commit()


def import_mapping(conn, csv_path: str) -> int:
    """Active chaque ligne de la feuille dont `odoo_propose` est renseigné.

    Pose le libellé FR curé et l'indice d'usage sur le compte Odoo correspondant. Renvoie
    le nombre de comptes activés. Permet au comptable de compléter les mappings « à confirmer »
    en remplissant la colonne odoo_propose puis en relançant l'import.
    """
    n = 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            code = (row.get("odoo_propose") or "").strip()
            if not code:
                continue
            activate(
                conn, code,
                label=(row.get("libelle_fr") or "").strip() or None,
                usage_hint=(row.get("usage_hint") or "").strip() or None,
            )
            n += 1
    return n

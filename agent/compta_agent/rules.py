"""Règles apprises : proposées par l'agent, validées (ou désactivées) par le comptable."""
from __future__ import annotations


def list_rules(conn, status: str | None = None) -> list[dict]:
    q = "select id, description, match_supplier, account_code, status, proposed_at from rules"
    args: tuple = ()
    if status:
        q += " where status = %s"
        args = (status,)
    return conn.execute(q + " order by proposed_at desc", args).fetchall()


def _one(conn, rule_id: str) -> str:
    rows = conn.execute("select id from rules where id::text like %s", (rule_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"Règle introuvable ou ambiguë : {rule_id}")
    return str(rows[0]["id"])


def validate_rule(conn, rule_id: str) -> None:
    rid = _one(conn, rule_id)
    conn.execute("update rules set status='validated', validated_at=now() where id=%s", (rid,))
    conn.commit()


def disable_rule(conn, rule_id: str) -> None:
    rid = _one(conn, rule_id)
    conn.execute("update rules set status='disabled' where id=%s", (rid,))
    conn.commit()

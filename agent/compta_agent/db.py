from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from . import config


def connect(actor: str = "cli", url: str | None = None) -> psycopg.Connection:
    """Connexion Postgres. `actor` est inscrit dans le journal d'audit (app.actor)."""
    conn = psycopg.connect(url or config.DATABASE_URL, row_factory=dict_row)
    conn.execute("select set_config('app.actor', %s, false)", (actor,))
    conn.commit()
    return conn

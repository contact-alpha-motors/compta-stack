"""Client de l'API « Assistant Caisse » (mini-app de virements) — voir GUIDE_BOT_VIREMENTS.md.

Backend Flask derrière Nginx, auth Supabase JWT. L'agent est un simple client : il crée un
ordre de virement (`POST /api/virements`) qui génère le PDF côté serveur et renvoie une URL
signée (24 h). L'agent ne déclenche JAMAIS un paiement — il produit l'ordre à signer.

La couche HTTP est injectable (`http=`) pour les tests : aucun réseau ni clé requis.
"""
from __future__ import annotations

import difflib
import os
import re

from . import config


class AssistantCaisseError(RuntimeError):
    pass


def _canonical_beneficiary(text: str) -> str | None:
    """Corrige une faute de frappe du bénéficiaire vers la liste connue (config.VIREMENT_BENEFICIARIES).
    « avifail » -> « avigail », « ndiang avigail » -> « avigail ». None si rien d'assez proche."""
    cands = config.VIREMENT_BENEFICIARIES
    t = (text or "").lower()
    if not cands or not t.strip():
        return None
    for c in cands:                                   # nom connu déjà présent dans le texte tapé
        if c and c in t:
            return c
    best, best_ratio = None, 0.0
    for w in re.findall(r"[a-zà-ÿ]+", t):             # sinon, mot le plus proche d'un nom connu
        for c in cands:
            ratio = difflib.SequenceMatcher(None, w, c).ratio()
            if ratio > best_ratio:
                best, best_ratio = c, ratio
    return best if best_ratio >= 0.72 else None


class _HttpxAdapter:
    """Adaptateur HTTP réel (httpx), importé tardivement : httpx n'est requis qu'en production."""

    def __init__(self, timeout: float = 30.0):
        import httpx

        self._c = httpx.Client(timeout=timeout)

    def get(self, url, headers=None):
        return self._c.get(url, headers=headers)

    def post(self, url, headers=None, json=None):
        return self._c.post(url, headers=headers, json=json)


def _check(resp, what: str):
    if resp.status_code >= 400:
        body = ""
        try:
            body = str(resp.json())
        except Exception:  # noqa: BLE001
            body = getattr(resp, "text", "")
        raise AssistantCaisseError(f"{what} : HTTP {resp.status_code} {body}")


class AssistantCaisse:
    def __init__(self, http=None, supabase_url=None, anon_key=None, api_base=None, email=None, password=None):
        self.supabase_url = (supabase_url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.anon_key = anon_key or os.environ.get("SUPABASE_ANON_KEY", "")
        self.api_base = (api_base or os.environ.get("API_BASE_URL", "http://app-assistante/api")).rstrip("/")
        self.email = email or os.environ.get("BOT_EMAIL", "")
        self.password = password or os.environ.get("BOT_PASSWORD", "")
        self._http = http or _HttpxAdapter()
        self._token: str | None = None

    # ---------------------------------------------------------------- auth
    def _login(self) -> str:
        r = self._http.post(
            f"{self.supabase_url}/auth/v1/token?grant_type=password",
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json={"email": self.email, "password": self.password},
        )
        _check(r, "Login Supabase")
        self._token = r.json()["access_token"]
        return self._token

    def _headers(self) -> dict:
        if not self._token:
            self._login()
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _get(self, path: str):
        r = self._http.get(f"{self.api_base}{path}", headers=self._headers())
        if r.status_code == 401:                 # token expiré : on se reconnecte une fois
            self._login()
            r = self._http.get(f"{self.api_base}{path}", headers=self._headers())
        _check(r, f"GET {path}")
        return r.json()

    def _post(self, path: str, payload: dict):
        r = self._http.post(f"{self.api_base}{path}", headers=self._headers(), json=payload)
        if r.status_code == 401:
            self._login()
            r = self._http.post(f"{self.api_base}{path}", headers=self._headers(), json=payload)
        _check(r, f"POST {path}")
        return r.json()

    # ---------------------------------------------------------------- endpoints
    def employees(self) -> list[dict]:
        return self._get("/employees")

    def comptes_emetteurs(self) -> list[dict]:
        return self._get("/virements/comptes-emetteurs")

    def resolve_employee(self, name: str) -> dict:
        """Trouve l'employé par nom (prenom/nom) et vérifie son éligibilité au virement."""
        if not name or not name.strip():
            raise AssistantCaisseError("Bénéficiaire non renseigné : impossible de créer l'ordre de virement")
        needle = _canonical_beneficiary(name) or name.strip().lower()   # corrige les fautes vers la liste connue
        matches = []
        for e in self.employees():
            full = f"{e.get('prenom', '')} {e.get('nom', '')}".strip().lower()
            alt = f"{e.get('nom', '')} {e.get('prenom', '')}".strip().lower()
            if needle in (full, alt) or needle in full or needle in alt:
                matches.append(e)
        if not matches:
            raise AssistantCaisseError(f"Aucun employé ne correspond à « {name} »")
        if len(matches) > 1:
            raise AssistantCaisseError(f"Plusieurs employés correspondent à « {name} » : préciser")
        emp = matches[0]
        if not emp.get("eligible_virement"):
            raise AssistantCaisseError(f"« {name} » n'est pas éligible aux virements")
        return emp

    def create_virement(self, employee_id: str, montant, motif: str | None = None,
                        virement_date: str | None = None, compte_emetteur: str = "alpha_motors") -> dict:
        """Crée l'ordre de virement et renvoie la réponse (id, reference, pdf_url...).

        compte_emetteur par défaut « alpha_motors » (règle projet : le serveur, lui, défaut sur
        alpha_invest — on envoie donc toujours explicitement le compte).
        """
        payload = {"employee_id": employee_id, "montant": int(montant), "compte_emetteur": compte_emetteur}
        if motif:
            payload["motif"] = motif
        if virement_date:
            payload["virement_date"] = virement_date
        return self._post("/virements", payload)

    def download_pdf(self, pdf_url: str) -> bytes:
        r = self._http.get(pdf_url, headers=None)
        _check(r, "Téléchargement PDF de l'ordre de virement")
        return r.content

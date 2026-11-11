"""Client LLM (OpenRouter, API compatible OpenAI) + appel d'outil avec réessais.

Le moteur par défaut est OpenRouter : un endpoint compatible OpenAI. Le choix du modèle
(Gemini par défaut) est piloté par la config, surchargeable sans toucher au code.

`call_tool` force l'usage d'un « outil » (function calling) et renvoie ses arguments déjà
désérialisés en dict. Les erreurs transitoires (réseau, 429, 5xx) sont réessayées avec un
backoff exponentiel — le reste des erreurs remonte telle quelle (le pipeline décide quoi en faire).

Le client est injectable : en test on passe un faux client, donc aucune clé ni réseau requis.
"""
from __future__ import annotations

import json
import time

from . import config


def make_client():
    """Client OpenAI pointé sur OpenRouter. Import tardif : openai n'est requis qu'en production."""
    from openai import OpenAI

    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.OPENROUTER_API_KEY or None)


def as_openai_tool(tool: dict) -> dict:
    """Convertit un outil {name, description, input_schema} vers le format function-calling OpenAI."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


_RETRYABLE_NAMES = {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
                    "ConnectError", "ConnectTimeout", "ReadTimeout", "PoolTimeout", "RemoteProtocolError"}


def _is_retryable(exc: Exception) -> bool:
    if type(exc).__name__ in _RETRYABLE_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 500:
        return True
    resp = getattr(exc, "response", None)                 # httpx.HTTPStatusError
    rstatus = getattr(resp, "status_code", None)
    return isinstance(rstatus, int) and (rstatus == 429 or rstatus >= 500)


_http = None


def _http_client():
    """Client HTTP keep-alive (réutilisé entre appels) pour l'endpoint « décisions » de Jev.
    La connexion persistante évite de repayer la poignée de main TLS à chaque message — c'est ce qui
    faisait paraître Jev lent (~1-2 s à froid contre ~0,65 s à chaud au benchmark)."""
    global _http
    if _http is None:
        import httpx

        _http = httpx.Client(timeout=30.0)
    return _http


def classify_decision(state_text: str, criteria: dict, *, model: str, instructions: str) -> tuple[str | None, float | None]:
    """Étage 1 — Jev (modèle de décision). Renvoie (intention, confiance). Lève si l'endpoint est injoignable
    (le routeur retombe alors sur le classifieur génératif de repli)."""
    cli = _http_client()
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    body = {"model": model, "state": state_text,
            "questions": {"intent": {"type": "choice", "instructions": instructions, "criteria": criteria}}}
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            resp = cli.post(config.LLM_DECISIONS_URL, headers=headers, json=body)
            resp.raise_for_status()
            ans = (resp.json().get("answers") or {}).get("intent") or {}
            return ans.get("choice"), ans.get("confidence")
        except Exception as exc:  # noqa: BLE001
            if attempt < config.LLM_MAX_RETRIES and _is_retryable(exc):
                time.sleep(config.LLM_BACKOFF_BASE * (2 ** attempt))
                continue
            raise


def complete(client, model: str, system: str, content, *, max_tokens: int = 400) -> str:
    """Complétion texte simple (sans outil) — utilisée pour les réponses conversationnelles (intent=chat)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(model=model, max_tokens=max_tokens, messages=messages)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt < config.LLM_MAX_RETRIES and _is_retryable(exc):
                time.sleep(config.LLM_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    return (resp.choices[0].message.content or "").strip()


def call_tool(client, model: str, system: str, content, tool: dict, *, max_tokens: int = 1000) -> dict:
    """Appelle le modèle en forçant `tool` et renvoie ses arguments (dict). {} si le modèle n'appelle rien.

    `content` est soit une chaîne, soit une liste de blocs (texte / image_url) au format OpenAI.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    oai_tool = as_openai_tool(tool)
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                tools=[oai_tool],
                tool_choice={"type": "function", "function": {"name": tool["name"]}},
            )
            break
        except Exception as exc:  # noqa: BLE001 — on réessaie si transitoire, sinon on relance
            if attempt < config.LLM_MAX_RETRIES and _is_retryable(exc):
                time.sleep(config.LLM_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    calls = resp.choices[0].message.tool_calls
    if not calls:
        return {}
    args = calls[0].function.arguments
    return json.loads(args) if isinstance(args, str) else dict(args or {})

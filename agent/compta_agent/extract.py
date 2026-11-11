"""Lecture d'une facture / d'un reçu (photo ou PDF) par un modèle de vision -> dictionnaire de champs.

Le moteur est OpenRouter (API compatible OpenAI). Les images sont envoyées telles quelles ; les PDF
sont rastérisés page par page en images avant envoi (Gemini lit mal les PDF via l'endpoint
OpenAI-compatible). Le contrat reste simple : obtenir libellé, montant, date et émetteur.
Tout ce qui est illisible reste à null : le code (pipeline.py) décide s'il faut poser une question.
"""
from __future__ import annotations

import base64
import io
from datetime import date
from pathlib import Path
from typing import Protocol

from . import config, llm


class Extractor(Protocol):
    def extract(self, path: Path) -> dict: ...


_NUM = {"type": ["number", "null"]}
_STR = {"type": ["string", "null"]}

EXTRACT_TOOL = {
    "name": "enregistrer_extraction",
    "description": "Enregistre les informations lues sur le document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type_document": {"type": "string", "enum": ["facture", "recu", "ticket", "bon_de_commande", "autre"]},
            "emetteur": {**_STR, "description": "Nom de l'entreprise ou de la personne qui émet le document."},
            "date": {**_STR, "description": "Date du document, format AAAA-MM-JJ."},
            "libelle": {**_STR, "description": "Description courte (12 mots max) de ce qui est acheté ou payé, en français."},
            "montant_total": {**_NUM, "description": "Total TTC à payer, nombre sans séparateur de milliers."},
            "montant_ht": _NUM,
            "tva": {**_NUM, "description": "Montant de TVA s'il est indiqué, sinon null."},
            "devise": {**_STR, "description": "Code devise (XOF, EUR, USD...). FCFA => XOF."},
            "numero_facture": _STR,
            "mode_paiement": {"type": "string", "enum": ["especes", "banque", "mobile_money", "non_paye", "inconnu"]},
            "lisibilite": {"type": "number", "description": "0 = illisible, 1 = parfaitement lisible."},
            "champs_incertains": {"type": "array", "items": {"type": "string"}},
            "question": {**_STR, "description": "UNE question courte en français si un champ essentiel est illisible ou douteux."},
        },
        "required": ["lisibilite", "mode_paiement"],
    },
}

SYSTEM = """Tu es l'assistant de saisie d'un service comptable (zone OHADA, monnaie : FCFA).
Tu lis UN document (facture, reçu, ticket) et tu appelles l'outil enregistrer_extraction.

Règles :
- N'invente jamais. Si une information est absente ou illisible, mets null.
- montant_total = le total TTC à payer, en nombre (ex. 118000). En FCFA il n'y a pas de décimales.
- Les dates écrites 03/04/2026 sont au format JOUR/MOIS/ANNÉE. Si le jour et le mois peuvent être inversés
  (ex. 04/05) sans indice, mets la date la plus probable ET ajoute "date" dans champs_incertains.
- libelle : ce qui est acheté, court et clair (ex. « Carburant véhicule », « Facture d'électricité de septembre »).
- emetteur : le nom de l'entreprise qui émet le document (pas le client).
- mode_paiement : "especes" si la mention payé en espèces / ticket de caisse, "banque" si virement/chèque/carte,
  "mobile_money" si Orange Money, MTN, Wave, Moov, etc., "non_paye" si facture à régler, sinon "inconnu".
- lisibilite : ton estimation honnête (photo floue, coupée, trop sombre => note basse).
- Si date, montant ou libellé est illisible ou douteux, rédige dans "question" UNE question courte et polie
  en français pour la personne qui a envoyé le document.
Réponds uniquement via l'outil."""

_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif"}
_MAX_BYTES = 4_500_000  # limite API ~5 Mo par image


def _shrink(data: bytes) -> bytes:
    from PIL import Image, ImageOps  # import tardif : seulement si nécessaire

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((2400, 2400))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _data_url(media: str, data: bytes) -> str:
    return f"data:{media};base64,{base64.standard_b64encode(data).decode()}"


def _image_block(media: str, data: bytes) -> dict:
    """Bloc image au format OpenAI (data URL en base64)."""
    if len(data) > _MAX_BYTES:
        data, media = _shrink(data), "image/jpeg"
    return {"type": "image_url", "image_url": {"url": _data_url(media, data)}}


def _pdf_to_images(data: bytes) -> list[tuple[str, bytes]]:
    """Rastérise les premières pages d'un PDF en PNG (PyMuPDF). Gemini lit mal les PDF bruts."""
    import fitz  # PyMuPDF ; import tardif

    pages: list[tuple[str, bytes]] = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for page in list(doc)[: config.PDF_MAX_PAGES]:
            pix = page.get_pixmap(dpi=config.PDF_RASTER_DPI)
            pages.append(("image/png", pix.tobytes("png")))
    finally:
        doc.close()
    if not pages:
        raise ValueError("PDF sans page exploitable")
    return pages


def build_blocks(path: Path) -> list[dict]:
    """Construit la liste de blocs de contenu (format OpenAI) pour un document. Un PDF => une image par page."""
    ext = path.suffix.lower()
    data = path.read_bytes()
    if ext == ".pdf":
        return [_image_block(media, raw) for media, raw in _pdf_to_images(data)]
    if ext in _IMAGE_TYPES:
        return [_image_block(_IMAGE_TYPES[ext], data)]
    raise ValueError(f"Format non pris en charge : {ext or '(sans extension)'} (jpg, png, webp, gif, pdf)")


class LlmExtractor:
    def __init__(self, client=None, model: str | None = None):
        self._client = client                       # création paresseuse : pas de clé requise tant qu'on ne lit rien
        self.model = model or config.EXTRACT_MODEL

    def extract(self, path: Path) -> dict:
        if self._client is None:
            self._client = llm.make_client()
        blocks = build_blocks(Path(path))
        content = [
            *blocks,
            {"type": "text", "text": f"Date du jour : {date.today().isoformat()}. Lis ce document."},
        ]
        data = llm.call_tool(self._client, self.model, SYSTEM, content, EXTRACT_TOOL, max_tokens=1500)
        if not data:
            raise RuntimeError("Le modèle n'a pas renvoyé d'extraction")
        return data

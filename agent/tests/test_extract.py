"""L'appel au modèle n'est pas testable sans clé : on vérifie la forme de la requête (OpenRouter /
API compatible OpenAI) et la lecture de la réponse. Le client est injecté -> aucun réseau ni clé."""
import json
from types import SimpleNamespace

import pytest

from compta_agent import extract
from compta_agent.categorize import make_llm_chooser


class FakeClient:
    """Imite client.chat.completions.create (format OpenAI / OpenRouter)."""

    def __init__(self, tool_args):
        self._args = tool_args
        self.kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(self._args)))
        message = SimpleNamespace(tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_extraction_image_envoie_une_image_et_force_loutil(tmp_path):
    f = tmp_path / "facture.jpg"
    f.write_bytes(b"\xff\xd8\xff fake")
    c = FakeClient({"emetteur": "CIE", "lisibilite": 0.9, "mode_paiement": "inconnu"})
    out = extract.LlmExtractor(client=c, model="google/gemini-3.8-flash").extract(f)
    assert out["emetteur"] == "CIE"
    k = c.kwargs
    assert k["model"] == "google/gemini-3.8-flash"
    assert k["tool_choice"]["function"]["name"] == "enregistrer_extraction"
    # messages[0] = system, messages[1] = user (blocs image + texte)
    block = k["messages"][1]["content"][0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_extraction_pdf_est_rasterise_en_image(tmp_path):
    fitz = pytest.importorskip("fitz")  # PyMuPDF
    f = tmp_path / "facture.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "FACTURE TEST")
    doc.save(f)
    doc.close()
    c = FakeClient({"lisibilite": 1, "mode_paiement": "inconnu"})
    extract.LlmExtractor(client=c).extract(f)
    block = c.kwargs["messages"][1]["content"][0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/")   # PDF -> image rastérisée


def test_format_inconnu_est_refuse(tmp_path):
    f = tmp_path / "facture.heic"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        extract.build_blocks(f)


def test_grosse_image_est_reduite(tmp_path):
    from PIL import Image
    import os
    f = tmp_path / "grosse.png"
    Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3)).save(f)     # bruit => PNG énorme (> 4,5 Mo)
    assert f.stat().st_size > 4_500_000
    block = extract.build_blocks(f)[0]
    assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")        # réduite + reconvertie en JPEG


def test_le_chooser_envoie_la_liste_des_comptes():
    c = FakeClient({"code": "6053", "confiance": 0.8, "raison": "carburant"})
    choose = make_llm_chooser(client=c, model="google/gemini-3.8-flash")
    out = choose([{"code": "6053", "label": "Fournitures non stockables, autres énergies", "usage_hint": "carburant"}],
                 {"emetteur": "Total", "libelle": "Carburant", "montant_total": 25000, "devise": "XOF", "type_document": "ticket"})
    assert out["code"] == "6053"
    # le listing des comptes est passé dans le contenu du message user (messages[1])
    assert "6053 | Fournitures non stockables" in c.kwargs["messages"][1]["content"]

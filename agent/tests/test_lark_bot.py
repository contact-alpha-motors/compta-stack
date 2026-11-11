"""Adaptateur Lark — parties pures (parsing d'événement) et traduction des relances en messages.
Le SDK lark_oapi n'est pas requis (seul le bot live l'importe)."""
import json
import uuid

from compta_agent import disbursements as d
from compta_agent import lark_bot


def test_text_from_content():
    assert lark_bot.text_from_content("text", json.dumps({"text": "demande 5000 taxi"})) == "demande 5000 taxi"
    assert lark_bot.text_from_content("image", json.dumps({"image_key": "img_x"})) == ""
    assert lark_bot.text_from_content("text", "pas du json") == ""


def test_build_inbound_texte_et_image():
    m = lark_bot.build_inbound("op-1", "text", json.dumps({"text": "aide"}))
    assert m.channel == "lark" and m.external_user_id == "op-1" and m.text == "aide" and m.attachments == []
    img = lark_bot.build_inbound("op-1", "image", json.dumps({"image_key": "k"}), ["/tmp/a.jpg"])
    assert img.text == "" and img.attachments == ["/tmp/a.jpg"]


def _seed(conn):
    ws = conn.execute("insert into workspaces(name, channel) values ('WS','lark') returning id").fetchone()["id"]
    def user(ext, name, role):
        return str(conn.execute(
            "insert into users(workspace_id, external_id, full_name, role) values (%s,%s,%s,%s) returning id",
            (ws, ext, name, role)).fetchone()["id"])
    ids = {"dem": user("open-dem", "Demandeur", "requester"),
           "cpt": user("open-cpt", "Comptable", "accountant")}
    conn.commit()
    return ids


def test_reminder_messages_relance_puis_escalade(conn):
    u = _seed(conn)
    r = d.create_request(conn, u["dem"], 60000, "Mission terrain", method="especes")
    rid = r.request_id
    # relance -> message direct au destinataire (par user_id)
    msgs = lark_bot.reminder_messages(conn, [{"request_id": rid, "action": "reminder", "user_id": u["dem"]}])
    assert len(msgs) == 1 and msgs[0]["open_id"] == "open-dem" and "justificatif" in msgs[0]["text"].lower()
    # escalade -> comptables/admins de l'entreprise
    esc = lark_bot.reminder_messages(conn, [{"request_id": rid, "action": "escalate"}])
    assert any(m["open_id"] == "open-cpt" and "retard" in m["text"].lower() for m in esc)

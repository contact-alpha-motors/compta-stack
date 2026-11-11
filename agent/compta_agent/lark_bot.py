"""Adaptateur Lark — connexion longue (WebSocket), aucune URL publique requise.

Le bot se connecte VERS Lark (mode « connexion longue »), reçoit les messages, les convertit
en `router.Inbound`, laisse le routeur (liste blanche + dispatch module A/B) décider, et renvoie
la réponse dans le chat. Un espace Lark = une instance (un couple app_id/secret).

Le SDK `lark_oapi` est importé tardivement : les fonctions pures (parsing d'événement, messages de
relance) et leurs tests ne le requièrent pas.
"""
from __future__ import annotations

import collections
import json
import threading
from datetime import datetime

from . import config, db, disbursements, lark_card, lark_dir, pipeline, reports
from .assistant_caisse import AssistantCaisse
from .categorize import Categorizer, make_llm_chooser
from .extract import LlmExtractor
from .router import Inbound, Router, make_llm_parser


# ------------------------------------------------------------------ parties pures (testables)

def text_from_content(msg_type: str, content_json: str) -> str:
    """Extrait le texte d'un message Lark. Les images/fichiers n'ont pas de texte."""
    if msg_type != "text":
        return ""
    try:
        return (json.loads(content_json or "{}").get("text") or "").strip()
    except (ValueError, TypeError):
        return ""


def build_inbound(open_id: str, msg_type: str, content_json: str, attachments: list[str] | None = None) -> Inbound:
    return Inbound(channel="lark", external_user_id=open_id,
                   text=text_from_content(msg_type, content_json), attachments=attachments or [])


def reminder_messages(conn, actions: list[dict]) -> list[dict]:
    """Traduit les actions de `run_reminders` en messages à envoyer : {open_id, text}.
    Relance = message direct au destinataire (récepteur ou décaisseur). Escalade = tous les comptables/admins (entreprise)."""
    out: list[dict] = []
    for a in actions:
        # Dépense enregistrée par texte dont le justificatif manque.
        if a.get("kind") == "expense_justif":
            if a.get("user_id"):
                u = conn.execute("select external_id from users where id = %s and external_id is not null",
                                 (a["user_id"],)).fetchone()
                if u:
                    out.append({"open_id": u["external_id"],
                                "text": f"⏰ Justificatif manquant pour la dépense « {a.get('label', '')} ». Merci de l'envoyer."})
            continue
        req = conn.execute(
            "select purpose, amount from disbursement_requests where id = %s", (a["request_id"],)).fetchone()
        if not req:
            continue
        montant = reports.fmt(req["amount"])
        if a["action"] == "reminder" and a.get("user_id"):
            u = conn.execute(
                "select external_id from users where id = %s and external_id is not null", (a["user_id"],)).fetchone()
            if u:
                out.append({"open_id": u["external_id"],
                            "text": f"⏰ Rappel : justificatif attendu pour « {req['purpose']} » ({montant})."})
        elif a["action"] == "escalate":
            accts = conn.execute(
                """select external_id from users where role in ('admin', 'accountant')
                   and active and external_id is not null""").fetchall()
            for ac in accts:
                out.append({"open_id": ac["external_id"],
                            "text": f"⚠️ Justificatif en retard non régularisé : « {req['purpose']} » ({montant})."})
    return out


# ------------------------------------------------------------------ bot live

class LarkBot:
    def __init__(self, app_id: str | None = None, app_secret: str | None = None):
        self.app_id = app_id or config.LARK_APP_ID
        self.app_secret = app_secret or config.LARK_APP_SECRET
        if not self.app_id or not self.app_secret:
            raise ValueError("LARK_APP_ID et LARK_APP_SECRET sont requis")
        self._tag = self.app_id[-6:]            # pour distinguer les logs quand plusieurs espaces tournent
        self._client = None
        self._parser = make_llm_parser()        # compréhension en langage naturel (repli structuré si IA indispo)
        self._seen: "collections.OrderedDict[str, bool]" = collections.OrderedDict()   # dédup des message_id
        self._lock = threading.Lock()
        # Objets persistants : le routeur garde la mémoire de conversation (slot-filling) entre les messages.
        self._router = Router(parse_fn=self._parser)
        self._extractor = LlmExtractor()
        self._categorizer = Categorizer(make_llm_chooser())
        self._caisse = AssistantCaisse()

    def _is_duplicate(self, message_id: str) -> bool:
        """Lark livre « au moins une fois » : on ignore un message déjà vu (anti-double-réponse)."""
        with self._lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = True
            if len(self._seen) > 2000:
                self._seen.popitem(last=False)
            return False

    def _lark(self):
        import lark_oapi as lark
        if self._client is None:
            self._client = (lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret)
                            .domain(config.LARK_DOMAIN).build())
        return lark, self._client

    # --- envoi ---
    def send(self, receive_id: str, text: str, id_type: str = "open_id") -> None:
        lark, client = self._lark()
        body = (lark.api.im.v1.CreateMessageRequestBody.builder()
                .receive_id(receive_id).msg_type("text")
                .content(json.dumps({"text": text})).build())
        req = (lark.api.im.v1.CreateMessageRequest.builder()
               .receive_id_type(id_type).request_body(body).build())
        client.im.v1.message.create(req)

    # --- envoi d'une carte interactive (boutons) ---
    def send_card(self, receive_id: str, card: dict, id_type: str = "open_id") -> None:
        lark, client = self._lark()
        body = (lark.api.im.v1.CreateMessageRequestBody.builder()
                .receive_id(receive_id).msg_type("interactive").content(json.dumps(card)).build())
        client.im.v1.message.create(
            lark.api.im.v1.CreateMessageRequest.builder().receive_id_type(id_type).request_body(body).build())

    def _push(self, notifs) -> None:
        """Pousse les notifications (cartes de validation / messages) vers d'autres utilisateurs."""
        for n in notifs or []:
            oid = n.get("open_id")
            if not oid:
                continue
            try:
                if n.get("type") == "approval_request":
                    self.send_card(oid, lark_card.approval_card(
                        n["request_id"], reports.fmt(n["amount"]), n["purpose"], n["requester_name"]))
                else:
                    self.send(oid, n.get("text", ""), id_type="open_id")
            except Exception as e:  # noqa: BLE001 — un destinataire injoignable (autre espace) ne bloque pas
                self._log(f"notif non délivrée à {oid}: {e!r}")

    # --- envoi d'un fichier (ex. PDF de l'ordre de virement) dans un chat ---
    def send_file(self, chat_id: str, path: str, file_name: str = "document.pdf") -> None:
        lark, client = self._lark()
        with open(path, "rb") as fh:
            up = client.im.v1.file.create(
                lark.api.im.v1.CreateFileRequest.builder().request_body(
                    lark.api.im.v1.CreateFileRequestBody.builder()
                    .file_type("pdf").file_name(file_name).file(fh).build()).build())
        file_key = up.data.file_key
        body = (lark.api.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("file").content(json.dumps({"file_key": file_key})).build())
        client.im.v1.message.create(
            lark.api.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build())

    # --- téléchargement d'une pièce jointe (image/fichier) ---
    def _download(self, message_id: str, file_key: str, res_type: str) -> str | None:
        lark, client = self._lark()
        try:
            req = (lark.api.im.v1.GetMessageResourceRequest.builder()
                   .message_id(message_id).file_key(file_key).type(res_type).build())
            resp = client.im.v1.message_resource.get(req)
            if not resp.success():
                return None
            import hashlib
            data = resp.file.read()
            sha = hashlib.sha256(data).hexdigest()
            ext = ".pdf" if res_type == "file" else ".jpg"
            dest = config.INVOICES_DIR / "lark" / f"{sha}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return str(dest)
        except Exception:  # noqa: BLE001 — une pièce illisible ne doit pas tuer le bot
            return None

    # --- réception d'un message ---
    def on_message(self, open_id: str, chat_id: str, msg_type: str, content_json: str, message_id: str) -> None:
        self._log(f"REÇU de {open_id} type={msg_type} id={message_id[:10]} contenu={content_json[:120]!r}")
        # Filet de sécurité : AUCUNE erreur de traitement ne doit tuer le thread (sinon le bot reste muet).
        try:
            attachments = []
            if msg_type in ("image", "file"):
                try:
                    content = json.loads(content_json or "{}")
                    key = content.get("image_key") or content.get("file_key")
                    if key:
                        path = self._download(message_id, key, "image" if msg_type == "image" else "file")
                        if path:
                            attachments.append(path)
                except (ValueError, TypeError):
                    pass
            inbound = build_inbound(open_id, msg_type, content_json, attachments)

            def _progress(text: str) -> None:        # accusé de réception immédiat (actions lentes : virement ~1 min)
                try:
                    self.send(chat_id, text, id_type="chat_id")
                    self._log(f"ACCUSÉ à {open_id}: {text[:80]!r}")
                except Exception as e:  # noqa: BLE001 — l'accusé ne doit jamais bloquer le traitement
                    self._log(f"échec accusé: {e!r}")

            conn = db.connect(actor=f"lark:{open_id}")
            try:
                reply = self._router.handle(
                    conn, inbound, extractor=self._extractor, categorizer=self._categorizer,
                    caisse=self._caisse, directory=self, progress=_progress)
            finally:
                conn.close()
            self._log(f"RÉPONSE à {open_id}: {reply.text[:120]!r}")
            self.send(chat_id, reply.text, id_type="chat_id")
            if getattr(reply, "file_path", None):
                try:
                    self.send_file(chat_id, reply.file_path, reply.file_name or "document.pdf")
                    self._log(f"FICHIER envoyé: {reply.file_path}")
                except Exception as e:  # noqa: BLE001 — l'échec d'envoi du fichier ne doit pas casser la réponse
                    self._log(f"échec envoi fichier: {e!r}")
            self._push(getattr(reply, "notifications", None))      # cartes de validation aux approbateurs, etc.
        except Exception:  # noqa: BLE001 — on log la trace et on répond poliment, mais le thread survit
            import traceback
            self._log("ERREUR on_message:\n" + traceback.format_exc())
            try:
                self.send(chat_id, "⛔ Oups, une erreur interne est survenue. Réessaie, ou reformule ta demande.",
                          id_type="chat_id")
            except Exception as e:  # noqa: BLE001
                self._log(f"échec envoi message d'erreur: {e!r}")

    def _handle_card(self, open_id: str, value: dict) -> None:
        self._log(f"CLIC bouton de {open_id}: {value}")
        conn = db.connect(actor=f"lark-card:{open_id}")
        try:
            reply = self._router.card_action(conn, open_id, value or {})
        finally:
            conn.close()
        try:
            self.send(open_id, reply.text, id_type="open_id")
        except Exception as e:  # noqa: BLE001
            self._log(f"carte: échec réponse: {e!r}")
        self._push(getattr(reply, "notifications", None))

    def resolve_person(self, name: str) -> dict | None:
        """Retrouve une personne dans l'annuaire Lark de cet espace (pour l'attribution de rôle)."""
        emps = lark_dir.list_employees(self.app_id, self.app_secret, config.LARK_DOMAIN)
        return lark_dir.resolve(name, emps)

    def _log(self, msg: str) -> None:
        print(f"[bot:{self._tag} {datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)

    # --- tâches planifiées ---
    def send_daily_report(self, chat_id: str) -> None:
        conn = db.connect(actor="lark:report")
        try:
            self.send(chat_id, reports.daily(conn), id_type="chat_id")
        finally:
            conn.close()

    def notify_reminders(self) -> int:
        conn = db.connect(actor="lark:reminders")
        try:
            actions = disbursements.run_reminders(conn) + pipeline.expense_reminders(conn)
            msgs = reminder_messages(conn, actions)
        finally:
            conn.close()
        for m in msgs:
            self.send(m["open_id"], m["text"], id_type="open_id")
        return len(msgs)

    # --- boucle principale (connexion longue) ---
    def start(self) -> None:
        import lark_oapi as lark

        def _handler(data) -> None:
            ev = data.event
            msg = ev.message
            sender_type = getattr(ev.sender, "sender_type", "user")
            # Ne traiter QUE les messages d'un vrai utilisateur (ignore bot/app/système -> évite les boucles).
            if sender_type != "user":
                self._log(f"IGNORÉ (expéditeur={sender_type}) id={msg.message_id[:10]}")
                return
            if self._is_duplicate(msg.message_id):
                self._log(f"IGNORÉ (doublon) id={msg.message_id[:10]}")
                return
            # Traitement en arrière-plan : le callback rend la main tout de suite (Lark acquitte vite).
            threading.Thread(
                target=self.on_message,
                args=(ev.sender.sender_id.open_id, msg.chat_id, msg.message_type, msg.content, msg.message_id),
                daemon=True).start()

        def _card(data) -> None:
            ev = data.event
            open_id = getattr(getattr(ev, "operator", None), "open_id", None)
            value = getattr(getattr(ev, "action", None), "value", None)
            if open_id:
                threading.Thread(target=self._handle_card, args=(open_id, value), daemon=True).start()

        builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(_handler)
        if hasattr(builder, "register_p2_card_action_trigger"):
            builder = builder.register_p2_card_action_trigger(_card)
        else:
            self._log("⚠️ Clic de carte non géré par ce SDK — repli : valider en tapant « approuve <réf> ».")
        handler = builder.build()
        self._log(f"écoute démarrée (app {self.app_id})")
        ws = lark.ws.Client(self.app_id, self.app_secret, domain=config.LARK_DOMAIN, event_handler=handler)
        ws.start()


def _serve_one(app_id: str, app_secret: str) -> None:
    LarkBot(app_id=app_id, app_secret=app_secret).start()


def serve_all(apps: list[tuple[str, str]]) -> None:
    """Lance une connexion longue par application Lark dans le MÊME conteneur.

    lark-oapi utilise une boucle asyncio globale au processus (singleton) : impossible de faire
    tourner plusieurs clients dans un seul processus. On lance donc un SOUS-PROCESSUS par app
    (fork), chacun avec sa propre boucle. Un seul service Docker, plusieurs process dedans. Bloque
    jusqu'à l'arrêt."""
    if len(apps) <= 1:
        _serve_one(*apps[0])
        return
    import multiprocessing as mp

    procs = [mp.Process(target=_serve_one, args=(app_id, secret), name=f"lark-{app_id[-6:]}")
             for app_id, secret in apps]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

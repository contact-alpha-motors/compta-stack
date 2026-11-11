"""Routeur conversationnel : liste blanche, parsing, slot-filling, dispatch caisse/banque."""
import uuid

from compta_agent import disbursements as d
from compta_agent import lark_card
from compta_agent.categorize import Categorizer
from compta_agent.router import Inbound, Router, _amount_from_text, make_llm_parser, parse


def seed(conn):
    ws = conn.execute("insert into workspaces(name, channel) values ('Alpha','lark') returning id").fetchone()["id"]
    def user(ext, name, role):
        conn.execute("insert into users(workspace_id, external_id, full_name, role) values (%s,%s,%s,%s)",
                     (ws, ext, name, role))
    user("op-dem", "Demandeur", "requester")
    user("op-cpt", "Comptable", "admin")       # admin = comptable
    user("op-mgr", "Manager", "manager")
    conn.commit()
    return ws


class FakeExtractor:
    def __init__(self, raw):
        self.raw = raw
    def extract(self, path):
        return dict(self.raw)


class FakeCaisse:
    def resolve_employee(self, name):
        from compta_agent.assistant_caisse import AssistantCaisseError
        if name and "laurent" in name.lower():
            return {"id": "emp-1", "eligible_virement": True}
        raise AssistantCaisseError("non éligible")
    def create_virement(self, employee_id, montant, motif=None, virement_date=None, compte_emetteur="alpha_motors"):
        return {"id": "vir-1", "reference": "REF", "pdf_url": "https://x/pdf"}
    def download_pdf(self, url):
        return b"%PDF test"


def chooser():
    return Categorizer(lambda accounts, data: {"code": "605200", "confiance": 0.9, "raison": "t"})


FACTURE = {"emetteur": "Total", "date": "2026-10-21", "libelle": "Carburant", "montant_total": 30000,
           "mode_paiement": "especes", "lisibilite": 0.9, "devise": "XOF"}


def _doc(conn):
    return str(conn.execute(
        "insert into documents(storage_path, sha256, status) values ('/x', %s, 'received') returning id",
        (uuid.uuid4().hex,)).fetchone()["id"])


def _uid(conn, ext):
    return str(conn.execute("select id from users where external_id=%s", (ext,)).fetchone()["id"])


# ---------------------------------------------------------------- parsing & liste blanche

def test_parse_demande():
    verb, args = parse("demande 50000 achat de pièces methode:especes")
    assert verb == "new" and args["amount"] == 50000 and args["payment_method"] == "especes"


def test_expediteur_inconnu_refuse(conn):
    seed(conn)
    r = Router().handle(conn, Inbound("lark", "intrus", "aide"))
    assert r.ok is False and "autorisé" in r.text


# ---------------------------------------------------------------- flux texte

def test_aide(conn):
    seed(conn)
    assert "virement" in Router().handle(conn, Inbound("lark", "op-dem", "aide")).text.lower()


def test_creation_sortie_de_caisse(conn):
    seed(conn)
    r = Router().handle(conn, Inbound("lark", "op-dem", "demande 50000 achat pièces methode:especes"))
    assert r.ok and "caisse" in r.text.lower()
    assert d.list_requests(conn)[0]["amount"] == 50000


def test_approbation_respecte_les_roles(conn):
    seed(conn)
    Router().handle(conn, Inbound("lark", "op-dem", "demande 50000 achat pièces methode:especes"))
    rid = str(d.list_requests(conn)[0]["id"])
    bad = Router().handle(conn, Inbound("lark", "op-dem", f"approuver {rid[:8]}"))
    assert bad.ok is False and "Séparation" in bad.text
    ok = Router().handle(conn, Inbound("lark", "op-cpt", f"approuver {rid[:8]}"))
    assert ok.ok and "approved" in ok.text


def test_slot_filling_virement_demande_le_beneficiaire(conn):
    seed(conn)
    # parseur simulé : 1er message = virement sans bénéficiaire ; 2e = un nom
    scripted = iter([("new", {"amount": 100000, "payment_method": "virement", "purpose": "achat pièces"}), ("", {})])
    bot = Router(parse_fn=lambda t: next(scripted))
    first = bot.handle(conn, Inbound("lark", "op-cpt", "..."), caisse=FakeCaisse())
    assert "bénéficiaire" in first.text.lower()
    second = bot.handle(conn, Inbound("lark", "op-cpt", "Laurent Ondobo"), caisse=FakeCaisse())
    assert second.ok and "virement" in second.text.lower()
    assert second.file_path                                    # le PDF de l'ordre est joint
    assert d.list_requests(conn)[0]["payment_method"] == "virement"


def test_message_incompris_reste_conversationnel(conn):
    seed(conn)
    r = Router().handle(conn, Inbound("lark", "op-dem", "coucou ça va"))
    assert "saisi" in r.text.lower()


class FakeDirectory:
    def __init__(self):
        self.sent = []
    def resolve_person(self, name):
        return {"open_id": "ou_avigail", "name": "Ndiang Avigail"} if name and "avigail" in name.lower() else None
    def send(self, open_id, text):
        self.sent.append((open_id, text))


def test_assign_role_par_admin_inscrit_et_souhaite_bienvenue(conn):
    seed(conn)
    dirx = FakeDirectory()
    bot = Router(parse_fn=lambda t: ("assign_role", {"person": "Avigail", "role": "manager"}))
    r = bot.handle(conn, Inbound("lark", "op-cpt", "Avigail est manager"), directory=dirx)
    assert r.ok and "manager" in r.text.lower()
    assert dirx.sent and "Ndiang Avigail" in dirx.sent[0][1]       # message de bienvenue envoyé
    row = conn.execute("select role from users where external_id='ou_avigail'").fetchone()
    assert row["role"] == "manager"


def test_assign_role_refuse_au_non_admin(conn):
    seed(conn)
    bot = Router(parse_fn=lambda t: ("assign_role", {"person": "Avigail", "role": "manager"}))
    r = bot.handle(conn, Inbound("lark", "op-mgr", "Avigail est manager"), directory=FakeDirectory())
    assert r.ok is False and "administrateur" in r.text.lower()


def test_depense_par_texte(conn):
    seed(conn)
    bot = Router(parse_fn=lambda t: ("expense", {"amount": 5000, "purpose": "carburant",
                                                 "payment_method": "especes", "tags": ["carburant"]}))
    r = bot.handle(conn, Inbound("lark", "op-dem", "j'ai payé 5000 de carburant"), categorizer=chooser())
    assert r.ok and "dépense enregistrée" in r.text.lower()
    assert conn.execute("select count(*) n from entries where status='posted'").fetchone()["n"] == 1


# ---------------------------------------------------------------- analyseur 2 étages (Jev -> Haiku)
# classify_fn / extract_fn / chat_fn sont injectés -> aucun appel réseau ni clé.

def test_2etages_order_bout_en_bout(conn):
    seed(conn)
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 100000, "beneficiary": "Laurent",
                                                         "purpose": "pièces", "payment_method": "virement"})
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-cpt", "vire 100000 à Laurent pour pièces"),
                                       caisse=FakeCaisse())
    assert r.ok and "virement" in r.text.lower() and r.file_path       # le PDF de l'ordre est joint


def test_2etages_order_sans_montant_demande_le_montant(conn):
    seed(conn)
    # X2 (anti-hallucination) : bénéficiaire présent, montant ABSENT -> le bot demande le montant, n'invente pas.
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"beneficiary": "vanauld", "payment_method": "virement"})
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-cpt", "fais un ordre de virement chez vanauld"),
                                       caisse=FakeCaisse())
    assert "montant" in r.text.lower()
    assert not d.list_requests(conn)                                   # rien créé tant que le montant manque


def test_2etages_expense(conn):
    seed(conn)
    parser = make_llm_parser(classify_fn=lambda t: "expense",
                             extract_fn=lambda i, t, k: {"amount": 15000, "purpose": "carburant",
                                                         "payment_method": "especes", "tags": ["carburant"]})
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-dem", "j'ai réglé 15000 de carburant"),
                                       categorizer=chooser())
    assert r.ok and "dépense enregistrée" in r.text.lower()


def test_2etages_help_et_unclear(conn):
    seed(conn)
    ph = make_llm_parser(classify_fn=lambda t: "help")
    assert "virement" in Router(parse_fn=ph).handle(conn, Inbound("lark", "op-dem", "comment ça marche ?")).text.lower()
    pu = make_llm_parser(classify_fn=lambda t: "unclear")
    assert "saisi" in Router(parse_fn=pu).handle(conn, Inbound("lark", "op-dem", "le truc là")).text.lower()


def test_2etages_chat_renvoie_la_reponse(conn):
    seed(conn)
    pc = make_llm_parser(classify_fn=lambda t: "chat", chat_fn=lambda t: "Bonjour 👋, comment puis-je aider ?")
    r = Router(parse_fn=pc).handle(conn, Inbound("lark", "op-dem", "salut"))
    assert r.ok and "Bonjour" in r.text


def test_2etages_classifieur_hs_repli_structure(conn):
    seed(conn)
    # Jev + Haiku indisponibles (classify_fn -> None) : repli sur l'analyseur structuré `parse`.
    parser = make_llm_parser(classify_fn=lambda t: None)
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-dem", "demande 50000 achat pièces methode:especes"))
    assert r.ok and d.list_requests(conn)[0]["amount"] == 50000


# ---------------------------------------------------------------- correctifs du 2026-10-23 (bug « 1 million » -> 1 F)

def test_amount_from_text_echelles():
    assert _amount_from_text("1 million, pour frais") == 1_000_000
    assert _amount_from_text("2,5 M") == 2_500_000
    assert _amount_from_text("50k") == 50_000
    assert _amount_from_text("50 000") == 50_000
    assert _amount_from_text("100f") == 100
    assert _amount_from_text("aucun chiffre ici") is None


def test_slot_filling_montant_en_lettres(conn):
    seed(conn)
    # « 1 million » donné en réponse au slot « montant » -> 1 000 000 (et non 1), via le repli déterministe.
    scripted = iter([("new", {"payment_method": "virement", "beneficiary": "Laurent", "purpose": "frais parc"}), ("", {})])
    bot = Router(parse_fn=lambda t: next(scripted))
    first = bot.handle(conn, Inbound("lark", "op-cpt", "fais un virement à Laurent pour le parc"), caisse=FakeCaisse())
    assert "montant" in first.text.lower()
    second = bot.handle(conn, Inbound("lark", "op-cpt", "1 million"), caisse=FakeCaisse())
    assert second.ok and second.file_path
    assert d.list_requests(conn)[0]["amount"] == 1_000_000


def test_virement_exige_le_motif(conn):
    seed(conn)
    # Montant + bénéficiaire présents mais PAS de motif -> le bot demande le motif (plus de défaut muet).
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 250000, "beneficiary": "Laurent",
                                                         "payment_method": "virement"})
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-cpt", "vire 250000 à Laurent"), caisse=FakeCaisse())
    assert "motif" in r.text.lower() or "objet" in r.text.lower()
    assert not d.list_requests(conn)


def test_virement_montant_suspect_demande_confirmation(conn):
    seed(conn)
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 1, "beneficiary": "Laurent",
                                                         "purpose": "frais", "payment_method": "virement"})
    bot = Router(parse_fn=parser)
    r1 = bot.handle(conn, Inbound("lark", "op-cpt", "vire 1 à Laurent pour frais"), caisse=FakeCaisse())
    assert "oui" in r1.text.lower() and "non" in r1.text.lower()      # demande de confirmation
    assert not d.list_requests(conn)                                 # rien créé avant confirmation
    r2 = bot.handle(conn, Inbound("lark", "op-cpt", "oui"), caisse=FakeCaisse())
    assert r2.ok and r2.file_path
    assert d.list_requests(conn)[0]["amount"] == 1


def test_virement_montant_suspect_annule_sur_non(conn):
    seed(conn)
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 1, "beneficiary": "Laurent",
                                                         "purpose": "frais", "payment_method": "virement"})
    bot = Router(parse_fn=parser)
    bot.handle(conn, Inbound("lark", "op-cpt", "vire 1 à Laurent pour frais"), caisse=FakeCaisse())
    r = bot.handle(conn, Inbound("lark", "op-cpt", "non"), caisse=FakeCaisse())
    assert "annule" in r.text.lower() and not d.list_requests(conn)


def test_slot_filling_montant_et_motif_ensemble(conn):
    seed(conn)
    # L'utilisateur donne montant ET motif dans la même réponse : refine (IA) capte les deux d'un coup.
    calls = {"n": 0}

    def classify(text):
        calls["n"] += 1
        return "order" if calls["n"] == 1 else "unclear"      # 2e tour : pas d'action -> pas de bascule
    parser = make_llm_parser(classify_fn=classify,
                             extract_fn=lambda i, t, k: {"beneficiary": "Laurent", "payment_method": "virement"})
    parser.refine = lambda i, t, k: {"amount": 1_000_000, "purpose": "frais de gestion du parc"}
    bot = Router(parse_fn=parser)
    first = bot.handle(conn, Inbound("lark", "op-cpt", "fais un virement à Laurent"), caisse=FakeCaisse())
    assert "montant" in first.text.lower()
    second = bot.handle(conn, Inbound("lark", "op-cpt", "1 million pour frais de gestion du parc"), caisse=FakeCaisse())
    assert second.ok and second.file_path
    row = d.list_requests(conn)[0]
    assert row["amount"] == 1_000_000 and "parc" in (row["purpose"] or "").lower()


def test_virement_beneficiaire_introuvable_message_clair(conn):
    seed(conn)
    # L'Assistant Caisse ne connaît pas « Zorro » -> message clair (⛔), PAS de crash, rien créé.
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 250000, "beneficiary": "Zorro",
                                                         "purpose": "frais", "payment_method": "virement"})
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-cpt", "vire 250000 à Zorro pour frais"),
                                       caisse=FakeCaisse())
    assert r.ok is False and "⛔" in r.text
    assert not d.list_requests(conn)


def test_virement_envoie_accuse_reception(conn):
    seed(conn)
    # action lente -> un accusé « en cours » est poussé AVANT le résultat (callback progress).
    parser = make_llm_parser(classify_fn=lambda t: "order",
                             extract_fn=lambda i, t, k: {"amount": 250000, "beneficiary": "Laurent",
                                                         "purpose": "frais", "payment_method": "virement"})
    acks = []
    r = Router(parse_fn=parser).handle(conn, Inbound("lark", "op-cpt", "vire 250000 à Laurent pour frais"),
                                       caisse=FakeCaisse(), progress=acks.append)
    assert r.ok and r.file_path
    assert acks and "prépare" in acks[0].lower()


def test_expense_pas_d_accuse(conn):
    seed(conn)
    # action rapide -> pas d'accusé (ciblé virement uniquement).
    parser = make_llm_parser(classify_fn=lambda t: "expense",
                             extract_fn=lambda i, t, k: {"amount": 5000, "purpose": "carburant",
                                                         "payment_method": "especes", "tags": ["carburant"]})
    acks = []
    Router(parse_fn=parser).handle(conn, Inbound("lark", "op-dem", "j'ai payé 5000 carburant"),
                                   categorizer=chooser(), progress=acks.append)
    assert not acks


# ---------------------------------------------------------------- cartes à boutons (2b)

def test_carte_approbation_structure():
    c = lark_card.approval_card("abc12345", "50 000 XOF", "carburant", "Jonas")
    btns = c["elements"][-1]["actions"]
    assert {b["value"]["a"] for b in btns} == {"approve", "reject"}
    assert all(b["value"]["rid"] == "abc12345" for b in btns)


def test_creation_caisse_notifie_les_approbateurs(conn):
    seed(conn)
    r = Router().handle(conn, Inbound("lark", "op-dem", "demande 50000 carburant methode:especes"))
    assert r.ok
    opens = {n["open_id"] for n in r.notifications}
    assert opens == {"op-cpt", "op-mgr"}                       # comptable + manager, pas le demandeur
    assert all(n["type"] == "approval_request" for n in r.notifications)


def test_clic_approuver(conn):
    seed(conn)
    Router().handle(conn, Inbound("lark", "op-dem", "demande 50000 carburant methode:especes"))
    rid = str(d.list_requests(conn)[0]["id"])
    r = Router().card_action(conn, "op-cpt", {"a": "approve", "rid": rid})
    assert r.ok and "approuv" in r.text.lower()
    assert d.get_request(conn, rid)["status"] == "approved"
    assert any(n["open_id"] == "op-dem" for n in r.notifications)     # demandeur notifié


def test_clic_refuser_demande_le_motif_puis_rejette(conn):
    seed(conn)
    Router().handle(conn, Inbound("lark", "op-dem", "demande 50000 carburant methode:especes"))
    rid = str(d.list_requests(conn)[0]["id"])
    bot = Router()
    r1 = bot.card_action(conn, "op-cpt", {"a": "reject", "rid": rid})
    assert "motif" in r1.text.lower()
    r2 = bot.handle(conn, Inbound("lark", "op-cpt", "budget dépassé"))   # même instance : la session porte le motif
    assert "rejet" in r2.text.lower()
    assert d.get_request(conn, rid)["status"] == "rejected"


# ---------------------------------------------------------------- pièces jointes

def test_piece_jointe_seule_enregistre_une_facture(conn, tmp_path):
    seed(conn)
    f = tmp_path / "facture.jpg"
    f.write_bytes(b"\xff\xd8\xff img")
    r = Router().handle(conn, Inbound("lark", "op-dem", "", [str(f)]),
                        extractor=FakeExtractor(FACTURE), categorizer=chooser())
    assert "entry_proposed" in r.text


def test_justificatif_joint_rapproche_une_avance(conn, tmp_path):
    seed(conn)
    dem, cpt = _uid(conn, "op-dem"), _uid(conn, "op-cpt")
    req = d.create_request(conn, dem, 30000, "Mission", method="especes")
    d.approve(conn, req.request_id, cpt)
    d.disburse(conn, req.request_id, cpt)
    rid = req.request_id
    f = tmp_path / "justif.jpg"
    f.write_bytes(b"\xff\xd8\xff j")
    r = Router().handle(conn, Inbound("lark", "op-dem", f"justificatif {rid[:8]}", [str(f)]),
                        extractor=FakeExtractor(FACTURE), categorizer=chooser())
    assert "justified" in r.text

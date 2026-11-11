"""Client « Assistant Caisse » : auth Supabase, résolution d'employé, création de virement. HTTP mocké."""
import pytest

from compta_agent.assistant_caisse import AssistantCaisse, AssistantCaisseError, _canonical_beneficiary


def test_canonical_beneficiary_corrige_les_fautes():
    assert _canonical_beneficiary("avifail") == "avigail"          # faute de frappe -> nom connu
    assert _canonical_beneficiary("Ndiang Avigail") == "avigail"   # nom complet -> prénom connu
    assert _canonical_beneficiary("LAURENT") == "laurent"
    assert _canonical_beneficiary("landri") == "landry"            # faute -> nom connu
    assert _canonical_beneficiary("Christophe Zorro") is None      # rien d'assez proche
    assert _canonical_beneficiary("") is None


class Resp:
    def __init__(self, status, data=None, content=b""):
        self.status_code = status
        self._data = data
        self.content = content
        self.text = str(data)

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, employees=None, fail_first_api=False):
        self.calls = []
        self.employees = employees if employees is not None else [
            {"id": "emp-1", "nom": "Ondobo", "prenom": "Laurent", "eligible_virement": True, "numero_compte": "123"},
            {"id": "emp-2", "nom": "Mballa", "prenom": "Jean", "eligible_virement": False},
        ]
        self._fail_first_api = fail_first_api
        self._api_hits = 0

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers, json))
        if "/auth/v1/token" in url:
            return Resp(200, {"access_token": "tok-abc", "refresh_token": "r"})
        if url.endswith("/virements"):
            return Resp(201, {"id": "vir-1", "reference": "Laurent#REF#1",
                              "pdf_url": "https://proj.supabase.co/storage/signed/pdf", "montant": json["montant"],
                              "compte_emetteur": json["compte_emetteur"]})
        return Resp(404, {"error": "not found"})

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        if url.endswith("/employees"):
            if self._fail_first_api and self._api_hits == 0:
                self._api_hits += 1
                return Resp(401, {"error": "jwt expired"})
            return Resp(200, self.employees)
        if url.endswith("/comptes-emetteurs"):
            return Resp(200, [{"key": "alpha_motors", "label": "Alpha Motors S.A."}])
        if "pdf" in url:
            return Resp(200, None, b"%PDF-1.4 ordre")
        return Resp(404, {"error": "not found"})


def client(http):
    return AssistantCaisse(http=http, supabase_url="https://proj.supabase.co", anon_key="anon",
                           api_base="http://api", email="bot@x", password="pw")


def test_login_puis_liste_employes():
    h = FakeHttp()
    emps = client(h).employees()
    assert len(emps) == 2
    assert any("/auth/v1/token" in c[1] for c in h.calls if c[0] == "POST")   # login effectué


def test_resolve_employee_eligible():
    emp = client(FakeHttp()).resolve_employee("Laurent Ondobo")
    assert emp["id"] == "emp-1"


def test_resolve_employee_non_eligible_refuse():
    with pytest.raises(AssistantCaisseError, match="éligible"):
        client(FakeHttp()).resolve_employee("Jean Mballa")


def test_resolve_employee_inconnu_refuse():
    with pytest.raises(AssistantCaisseError, match="Aucun employé"):
        client(FakeHttp()).resolve_employee("Personne Inexistante")


def test_create_virement_envoie_alpha_motors_et_montant_entier():
    h = FakeHttp()
    resp = client(h).create_virement("emp-1", 2000000, motif="salaire", compte_emetteur="alpha_motors")
    assert resp["id"] == "vir-1" and resp["pdf_url"].startswith("https://")
    post = [c for c in h.calls if c[0] == "POST" and c[1].endswith("/virements")][0]
    body = post[3]
    assert body["compte_emetteur"] == "alpha_motors" and body["montant"] == 2000000 and isinstance(body["montant"], int)


def test_token_expire_declenche_une_reconnexion():
    h = FakeHttp(fail_first_api=True)
    emps = client(h).employees()          # 1er GET -> 401 -> re-login -> 2e GET -> 200
    assert len(emps) == 2
    logins = [c for c in h.calls if c[0] == "POST" and "/auth/v1/token" in c[1]]
    assert len(logins) >= 2


def test_download_pdf():
    assert client(FakeHttp()).download_pdf("https://proj.supabase.co/storage/signed/pdf") == b"%PDF-1.4 ordre"

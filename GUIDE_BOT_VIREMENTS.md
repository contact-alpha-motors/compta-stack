# Guide d'intégration — Bot d'ordres de virement (Lark)

Ce document explique à l'agent qui construit le bot Lark **comment se connecter à l'API
« Assistant Caisse » existante** pour créer des ordres de virement, et **ce qu'il doit
préparer** côté Supabase. Le backend n'a **aucune modification à subir** : le bot est un
simple client de l'API REST déjà en place.

---

## ⚠️ Tâche préalable à exécuter par l'agent

**Créer un utilisateur Supabase dédié au bot :**

- **Email :** `laurent@alphamotors-cameroun.com`
- **Mot de passe :** fort, généré aléatoirement, stocké dans les variables d'environnement
  du bot (jamais en dur dans le code, jamais commité).
- **`email_confirm` : true** (l'utilisateur doit être confirmé, sinon le login par
  mot de passe échoue).

Deux moyens au choix :

**A. Dashboard Supabase (le plus simple)**
Projet Supabase `alphamotors` → **Authentication → Users → Add user** →
saisir l'email + mot de passe → cocher **Auto Confirm User**.

**B. Admin API (service role key)**
```http
POST {SUPABASE_URL}/auth/v1/admin/users
Headers:
  apikey: {SUPABASE_SERVICE_KEY}
  Authorization: Bearer {SUPABASE_SERVICE_KEY}
  Content-Type: application/json
Body:
  { "email": "laurent@alphamotors-cameroun.com", "password": "<mdp-fort>", "email_confirm": true }
```

> Ce user n'a pas de rôle particulier : l'API accepte **tout utilisateur Supabase valide**.
> Il aura donc les mêmes droits qu'un utilisateur de l'app. Protéger ses identifiants.

---

## 1. Authentification

Tous les endpoints de l'API sont protégés et exigent un **JWT utilisateur Supabase** dans
le header `Authorization: Bearer <access_token>`.

Il n'existe **pas** de route de login dans le backend : le token s'obtient **directement
auprès de Supabase Auth**.

### Obtenir un token
```http
POST {SUPABASE_URL}/auth/v1/token?grant_type=password
Headers:
  apikey: {SUPABASE_ANON_KEY}
  Content-Type: application/json
Body:
  { "email": "laurent@alphamotors-cameroun.com", "password": "<mdp>" }
```
Réponse :
```json
{
  "access_token": "eyJ...",
  "refresh_token": "...",
  "expires_in": 3600,
  "token_type": "bearer"
}
```

### Durée de vie / rafraîchissement
- `access_token` valable ~1 h (`expires_in`).
- À l'expiration : soit re-login (même requête), soit rafraîchir :
```http
POST {SUPABASE_URL}/auth/v1/token?grant_type=refresh_token
Headers: apikey: {SUPABASE_ANON_KEY}, Content-Type: application/json
Body: { "refresh_token": "<refresh_token>" }
```
Le bot doit gérer le renouvellement automatiquement avant chaque appel (ou sur 401).

---

## 2. URL de base

| Contexte | Base URL |
|---|---|
| **Même serveur (recommandé)** — le bot rejoint le réseau Docker `proxy` | `http://app-assistante/api` |
| **Externe** — via le domaine public | `https://<ton-domaine>/api` |

- Nginx route `/api` vers le backend Flask. Le bot n'a pas besoin de connaître le port interne.
- CORS = `*`, aucune contrainte d'origine.

### Variables d'environnement attendues côté bot
```
SUPABASE_URL=https://<projet>.supabase.co
SUPABASE_ANON_KEY=eyJ...            # clé anon (publique), pour le login
API_BASE_URL=http://app-assistante/api
BOT_EMAIL=laurent@alphamotors-cameroun.com
BOT_PASSWORD=<mdp-fort>
LARK_APP_ID=...
LARK_APP_SECRET=...
ALLOWED_LARK_USERS=...              # liste blanche des utilisateurs autorisés (voir §6)
```
> ⚠️ Ne jamais mettre `SUPABASE_SERVICE_KEY` dans le bot. Le login se fait avec l'**anon key**.

---

## 3. Endpoints

Tous requièrent le header `Authorization: Bearer <access_token>`.

### 3.1 Lister les employés → résoudre `employee_id`
```http
GET /api/employees
```
Renvoie la liste des employés actifs. Le bot matche le nom demandé pour récupérer `id`.
Champs utiles : `id`, `nom`, `prenom`, `fonction`, `eligible_virement`, `numero_compte`, `genre`.

> L'employé ciblé doit avoir **`eligible_virement = true`**, sinon la création renverra une
> erreur 400 (« Cet employé n'est pas éligible aux virements »).

### 3.2 Lister les comptes émetteurs
```http
GET /api/virements/comptes-emetteurs
```
```json
[
  { "key": "alpha_invest", "label": "Alpha Invest",     "numero": "10005 00001 09244611001 82" },
  { "key": "alpha_motors", "label": "Alpha Motors S.A.", "numero": "10005 00001 00000132693 09" }
]
```

### 3.3 Créer un virement (+ PDF)
```http
POST /api/virements
Content-Type: application/json
```
```json
{
  "employee_id": "<uuid>",
  "montant": 2000000,
  "compte_emetteur": "alpha_motors",
  "motif": "salaire octobre",
  "virement_date": "2026-10-20"
}
```
| Champ | Requis | Notes |
|---|---|---|
| `employee_id` | ✅ | UUID obtenu via `GET /api/employees` |
| `montant` | ✅ | Nombre (FCFA) |
| `compte_emetteur` | ⚠️ à envoyer | `"alpha_motors"` ou `"alpha_invest"` — **voir règle de défaut ci-dessous** |
| `motif` | ❌ | Texte libre, apparaît dans le PDF |
| `virement_date` | ❌ | ISO `AAAA-MM-JJ`, défaut = aujourd'hui |

> **Règle de défaut imposée par le projet : Alpha Motors par défaut.**
> Le défaut **côté serveur** est `alpha_invest`. Le bot doit donc **toujours** envoyer
> `compte_emetteur` lui-même : `"alpha_motors"` sauf si l'utilisateur précise explicitement
> « Alpha Invest ».

> La **référence** (ex. `Laurent ONDOBO#Chef#virement n.0001`) est **générée
> automatiquement** côté serveur. Ne pas l'envoyer.

**Réponse 201 :**
```json
{
  "id": "<uuid>",
  "reference": "Laurent ONDOBO#...# virement n.0001",
  "montant": 2000000,
  "motif": "salaire octobre",
  "virement_date": "2026-10-20",
  "compte_emetteur": "alpha_motors",
  "pdf_url": "https://<projet>.supabase.co/storage/v1/object/sign/invoices/...",
  "locked": false,
  "created_by": "<user_id-du-bot>",
  "created_at": "..."
}
```
`pdf_url` = **URL signée valable 24 h** → le bot peut la renvoyer / joindre le PDF dans Lark.

### 3.4 Modifier / confirmer (optionnel)
```http
PATCH /api/virements/<id>
```
- Champs modifiables : `montant`, `motif`, `virement_date`, `compte_emetteur` (régénère le PDF).
- Envoyer `"locked": true` pour **confirmer/verrouiller** le virement.
- Un virement verrouillé ne peut plus être modifié ni supprimé (409).

### 3.5 Supprimer (optionnel)
```http
DELETE /api/virements/<id>
```
Échoue avec 409 si le virement est verrouillé.

---

## 4. Flux recommandé pour le bot

1. L'utilisateur écrit dans Lark : *« vire 2M à Laurent Ondobo, motif salaire »*.
2. Le bot vérifie que l'utilisateur Lark est dans la **liste blanche** (sinon ignore).
3. Parsing (NLP ou commande structurée) → `{ nom, montant, compte_emetteur?, motif? }`.
   Défaut `compte_emetteur = "alpha_motors"`.
4. `GET /api/employees` → matcher le nom → `employee_id` (vérifier `eligible_virement`).
5. **Récapitulatif + confirmation** (bouton Lark) avant toute création — opération financière.
6. Sur confirmation → `POST /api/virements`.
7. Renvoyer le PDF (`pdf_url`) dans la conversation Lark.

---

## 5. Impact sur l'existant

- **Aucune modification du backend.** Le bot est un client API supplémentaire.
- **Registre partagé :** les virements créés par le bot s'écrivent dans la **même table
  `virements`** et le même bucket `invoices`. Ils **apparaissent dans le PWA**, et inversement.
- **Traçabilité :** `created_by` = user_id du bot → permet de distinguer « créé par le bot ».
- **Compteur partagé :** la numérotation (`virement n.XXXX`, par employé) est commune
  bot + app — pas de collision, séquence unique.
- **Charge :** négligeable (quelques requêtes par virement).

---

## 6. Sécurité (impératif — opération financière)

- **Liste blanche Lark :** seuls des `user_id` / `chat_id` Lark explicitement autorisés
  peuvent déclencher un virement. Tout le reste est ignoré.
- **Étape de confirmation obligatoire** avant `POST` (récap montant + bénéficiaire + compte).
- **Identifiants du bot** (`BOT_PASSWORD`) en variables d'environnement, jamais en dur,
  jamais commités.
- **Jamais** d'`SUPABASE_SERVICE_KEY` dans le bot.
- Journaliser chaque virement créé (qui, quand, montant, référence) côté bot pour audit.

---

## Référence code backend
- Logique virements : `backend/routes/virements.py`
- Auth (validation du Bearer token) : `backend/utils/auth.py`
- Employés : `backend/routes/employees.py`
- Noms de variables d'env : `.env.example`

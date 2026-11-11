from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://agent_app@localhost:5432/compta")
INVOICES_DIR = Path(os.environ.get("INVOICES_DIR", "./data/invoices"))

# Moteur LLM : OpenRouter (API compatible OpenAI). Clé + endpoint + modèles surchargeables sans toucher au code.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "google/gemini-3.8-flash")        # lecture de factures (images)
CATEGORIZE_MODEL = os.environ.get("CATEGORIZE_MODEL", "google/gemini-3.8-flash")  # proposition de compte

# Routeur conversationnel en 2 étages (benchmark du 2026-10-23) :
#  Étage 1 — CLASSER l'intention : Jev (modèle de décision, endpoint /api/alpha/decisions) en primaire
#    — le plus rapide et le moins cher, et il renvoie une confiance calibrée. Repli génératif Haiku 5.5
#    si l'endpoint alpha tombe ou si la confiance est sous le seuil.
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "typesafe/jev-1.13")
CLASSIFY_FALLBACK_MODEL = os.environ.get("CLASSIFY_FALLBACK_MODEL", "anthropic/claude-haiku-5.5")
CLASSIFY_CONFIDENCE_MIN = float(os.environ.get("CLASSIFY_CONFIDENCE_MIN", "0.6"))
LLM_DECISIONS_URL = os.environ.get("LLM_DECISIONS_URL", "https://openrouter.ai/api/alpha/decisions")
#  Étage 2 — EXTRAIRE les champs (order/expense) + répondre en chat : Haiku 5.5 (précis, rapide,
#    zéro hallucination au bench — laisse les champs absents à null au lieu d'inventer).
ROUTER_EXTRACT_MODEL = os.environ.get("ROUTER_EXTRACT_MODEL", "anthropic/claude-haiku-5.5")
ROUTER_CHAT_MODEL = os.environ.get("ROUTER_CHAT_MODEL", "anthropic/claude-haiku-5.5")

# Réessais sur erreurs transitoires du modèle (réseau, 429, 5xx)
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
LLM_BACKOFF_BASE = float(os.environ.get("LLM_BACKOFF_BASE", "1.0"))  # secondes, doublé à chaque tentative

# Rastérisation PDF -> images avant envoi au modèle (Gemini lit mal les PDF via l'endpoint OpenAI-compatible)
PDF_MAX_PAGES = int(os.environ.get("PDF_MAX_PAGES", "5"))
PDF_RASTER_DPI = int(os.environ.get("PDF_RASTER_DPI", "150"))

CURRENCY = os.environ.get("CURRENCY", "XOF")

# Rôles de comptes utilisés par la logique déterministe (pipeline). Surchargeables par env :
# la logique ne code plus aucun numéro en dur, elle s'adapte au plan comptable actif.
# Défauts = codes Odoo (plan source). Les comptes-rôles 401/421/521/571 sont des défauts
# justifiés par le libellé, à CONFIRMER par le comptable (voir initdb/plan-comptable-mapping.csv).
ACCT_TVA_IMMO = os.environ.get("ACCT_TVA_IMMO", "445100")        # TVA récupérable sur immobilisations
ACCT_TVA_ACHATS = os.environ.get("ACCT_TVA_ACHATS", "445200")    # TVA récupérable sur achats (biens, marchandises)
ACCT_TVA_SERVICES = os.environ.get("ACCT_TVA_SERVICES", "445400")  # TVA récupérable sur services extérieurs et autres charges
ACCT_CASH = os.environ.get("ACCT_CASH", "571100")               # caisse (espèces) — « Cash in national currency »
ACCT_BANK = os.environ.get("ACCT_BANK", "521001")               # banque (virement / carte / mobile money) — « Bank »
ACCT_SUPPLIER = os.environ.get("ACCT_SUPPLIER", "401100")        # fournisseurs, dettes en compte — « Suppliers »
ACCT_ADVANCE_PERSONNEL = os.environ.get("ACCT_ADVANCE_PERSONNEL", "421100")  # avances au personnel (module B) — « Personnel, advances »

# Décaissements (module B). Seuils marqués « à CONFIRMER avec le comptable » — surchargeables par env.
# Au-dessus de ce montant, une double validation est exigée :
DISBURSEMENT_DOUBLE_APPROVAL_THRESHOLD = Decimal(os.environ.get("DISBURSEMENT_DOUBLE_APPROVAL_THRESHOLD", "1000000"))  # XOF (à confirmer)
# En dessous de ce montant, la pièce justificative n'est pas exigée (0 = pièce TOUJOURS exigée) :
DISBURSEMENT_PIECE_EXEMPT_BELOW = Decimal(os.environ.get("DISBURSEMENT_PIECE_EXEMPT_BELOW", "0"))
# Délai de justificatif après réception des fonds, et nombre de relances avant alerte comptable :
JUSTIF_DELAY_HOURS = int(os.environ.get("JUSTIF_DELAY_HOURS", "24"))
JUSTIF_MAX_REMINDERS = int(os.environ.get("JUSTIF_MAX_REMINDERS", "2"))
# Garde-fou de saisie : en dessous de ce montant, un ordre de virement demande une confirmation
# (un virement de 1 FCFA = quasi toujours une faute de frappe, ex. « 1 million » mal lu).
VIREMENT_CONFIRM_BELOW = Decimal(os.environ.get("VIREMENT_CONFIRM_BELOW", "1000"))
# Bénéficiaires connus des ordres de virement. Sert à CORRIGER les fautes de frappe du nom tapé
# (« avifail » -> « avigail ») avant de résoudre l'employé côté Assistant Caisse. Surchargeable par env.
VIREMENT_BENEFICIARIES = [s.strip().lower() for s in os.environ.get(
    "VIREMENT_BENEFICIARIES", "avigail,laurent,vanauld,jonas,landry").split(",") if s.strip()]

# Bot Lark (connexion longue — aucune URL publique requise). Un espace = une instance.
LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
# 2e espace Lark (optionnel) — servi par le MÊME conteneur, dans un autre thread.
LARK_APP_ID_2 = os.environ.get("LARK_APP_ID_2", "")
LARK_APP_SECRET_2 = os.environ.get("LARK_APP_SECRET_2", "")
LARK_REPORT_CHAT_ID = os.environ.get("LARK_REPORT_CHAT_ID", "")   # chat où envoyer le rapport quotidien
# Domaine : Lark international par défaut (open.larksuite.com). Feishu/Chine = https://open.feishu.cn
LARK_DOMAIN = os.environ.get("LARK_DOMAIN", "https://open.larksuite.com")

# Garde-fous de lecture
MIN_LEGIBILITY = float(os.environ.get("MIN_LEGIBILITY", "0.5"))        # en dessous : on demande une précision
MAX_INVOICE_AGE_DAYS = int(os.environ.get("MAX_INVOICE_AGE_DAYS", "400"))  # facture plus vieille : on demande

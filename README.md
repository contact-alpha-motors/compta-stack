# Stack agent comptable

Conteneurs : `db` (Postgres 16), `studio` (Adminer, lecture seule), `backup` (pg_dump chaque nuit).
Le conteneur `agent` est prévu mais commenté : on l'active quand le service est prêt.

## Démarrage (en SSH sur le VPS)

```sh
cp .env.example .env        # puis remplir les 3 mots de passe (openssl rand -hex 16)
chmod +x initdb/02-roles.sh
docker compose up -d
docker compose ps           # db doit être "healthy"
```

Le dossier `initdb/` n'est exécuté **qu'au premier démarrage** (base vide).
Pour repartir de zéro en test : `docker compose down -v` (efface les données !).

## Plan comptable

La **source unique** est le plan comptable Odoo d'Alpha Motors (`plan_comptable_odoo.csv`, codes à
6 chiffres). `initdb/03-plan-comptable.sql` importe les **1140 comptes Odoo** en « pending » (l'agent
n'impute jamais dans un compte non « active »). `initdb/04-activer-plan-comptable.sql` active le
**sous-ensemble validé** : les comptes dont la correspondance SYSCOHADA→Odoo est sans ambiguïté, plus
les comptes-rôles du moteur (fournisseurs, banque, caisse, avances) — avec leur libellé FR et leur
indice d'usage métier.

Les comptes dont la correspondance est ambiguë (un compte SYSCOHADA court → plusieurs sous-comptes
Odoo) ou absente restent « pending » : ils sont listés dans **`initdb/plan-comptable-mapping.csv`**
pour que le comptable choisisse. Il complète la colonne `odoo_propose` puis active en masse :

```sh
docker compose run --rm -v "$PWD/initdb:/map:ro" agent accounts import-mapping /map/plan-comptable-mapping.csv
```

Activation / désactivation à l'unité, et consultation :

```sh
docker compose run --rm agent accounts activate 622400
docker compose run --rm agent accounts disable 401200
docker compose run --rm agent accounts list --statut active --prefixe 60
```

> ⚠️ Les comptes-rôles activés d'office (`401100` fournisseurs, `521001` banque, `571100` caisse,
> `421100` avances au personnel) sont des **défauts à confirmer** par le comptable ; ils pilotent les
> contrepassations du moteur et se surchargent par variables d'environnement (`ACCT_SUPPLIER`, etc.).

## Agent : lire des factures (prototype en ligne de commande)

Mettre `OPENROUTER_API_KEY` dans `.env`, déposer des factures (jpg, png, pdf) dans un dossier `inbox/`, puis :

```sh
docker compose build agent
docker compose run --rm -v "$PWD/inbox:/inbox:ro" agent ingest /inbox/facture1.jpg /inbox/facture2.pdf
docker compose run --rm agent review                      # ce qui attend le comptable
docker compose run --rm agent confirm <id>                # valider une écriture
docker compose run --rm agent confirm <id> --compte 6053  # valider en corrigeant le compte
docker compose run --rm agent fix <document> --date 2026-10-14 --montant 25000
docker compose run --rm agent rules                       # règles apprises ; `rules validate <id>`
docker compose run --rm agent report daily                # ou weekly
```

Ce que fait `ingest` : lit libellé, montant, date, émetteur (+ TVA, mode de paiement si visibles).
Si la photo est floue ou qu'un champ essentiel manque, il pose une question (rien n'est enregistré).
Sinon il propose une écriture **brouillon** ; rien n'est validé tant que le comptable ne confirme pas.

Tests (sans clé API, avec un Postgres de test) :
`TEST_ADMIN_URL=postgresql://postgres@localhost:5432/postgres python -m pytest agent/tests`

Si la base existe déjà (stack déjà démarrée avant l'arrivée du plan comptable), les scripts `initdb/`
ne se rejouent pas : exécuter à la main `03-plan-comptable.sql` puis `04-activer-plan-comptable.sql`.

## Agent : décaissements (module B)

Demande de fonds → pièce obligatoire → approbation (séparation des rôles, double validation au-dessus
d'un seuil) → ordre de virement (Assistant Caisse) → paiement (écriture d'avance) → justificatif rapproché.
L'agent ne déclenche jamais un paiement ; tout est validé par un humain.

```sh
docker compose run --rm agent request new --montant 2000000 --objet "avance mission" --demandeur <id> --beneficiaire "Laurent Ondobo"
docker compose run --rm agent request attach <demande> <document>        # pièce (devis/proforma/facture)
docker compose run --rm agent request approve <demande> --par <id>       # (2 fois si > seuil, approbateurs distincts)
docker compose run --rm agent request order <demande>                    # ordre de virement + PDF (Assistant Caisse)
docker compose run --rm agent request pay <demande>                      # paiement constaté -> écriture d'avance
docker compose run --rm agent request reconcile <demande> <justificatif> # rapproche le justificatif de l'avance
docker compose run --rm agent reminders                                  # relance les justificatifs en retard
```

Décaissement en espèces / mobile money : `request new ... --methode especes` (pas d'ordre de virement,
on va directement à `request pay`). La config `ACCT_*` et les seuils (`DISBURSEMENT_*`, `JUSTIF_*`) se
règlent par variables d'environnement. L'« Assistant Caisse » exige `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`BOT_EMAIL`, `BOT_PASSWORD` (voir `GUIDE_BOT_VIREMENTS.md`).

Le module `router.py` route un message (Lark/WhatsApp/web) vers ces commandes avec liste blanche des
utilisateurs.

## Bot Lark (service permanent)

Le bot utilise la **connexion longue** de Lark : il se connecte *vers* Lark, donc **aucune URL publique
ni webhook** à exposer. Il faut seulement, dans la console développeur Lark, régler l'abonnement aux
événements sur « connexion longue » (long connection), ajouter le bot aux conversations, et renseigner
`LARK_APP_ID` / `LARK_APP_SECRET` dans `.env`.

```sh
# autoriser les utilisateurs (liste blanche) — open_id Lark de chacun
docker compose run --rm agent users add --openid ou_xxx --nom "Laurent" --role accountant
docker compose run --rm agent users add --openid ou_yyy --nom "Agent" --role requester

# lancer le bot en service permanent
docker compose --profile bot up -d bot
```

Rapports / relances automatiques : via cron (hôte ou planificateur), par ex. chaque jour à 18 h et des
relances toutes les heures :

```cron
0 18 * * *   docker compose -f /chemin/docker-compose.yml run --rm agent lark report
0  *  * * *   docker compose -f /chemin/docker-compose.yml run --rm agent lark remind
```

Un espace Lark = une instance du bot. Pour le 2ᵉ espace, dupliquer le service `bot` avec l'app_id/secret
de l'autre application. Tout utilisateur Lark non inscrit (liste blanche) est ignoré.

## Accès au studio

Adminer écoute sur `127.0.0.1:8080` uniquement. Publiez-le via votre reverse proxy avec
authentification, ou passez par un tunnel SSH : `ssh -L 8080:127.0.0.1:8080 utilisateur@vps`.
Connexion : serveur `db`, utilisateur `studio_ro`, base `compta`.

## Sauvegardes

Les dumps sont dans le volume `backups` (14 jours, 8 semaines, 12 mois).
**À faire** : copier ce volume + le volume `invoices` hors du VPS (rclone vers Drive/S3, par exemple).
Un test de restauration au moins une fois est indispensable :
`docker compose exec -T db psql -U compta_admin -d compta < dump.sql` (sur une base de test).

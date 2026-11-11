#!/bin/sh
# Crée les rôles applicatifs APRÈS le schéma (01-schema.sql).
#  - agent_app : utilisé par l'agent. Peut lire/écrire, mais pas supprimer l'audit ni changer le schéma.
#  - studio_ro : lecture seule, pour Adminer / consultation humaine.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
create role agent_app login password '${AGENT_DB_PASSWORD}';
create role studio_ro login password '${STUDIO_DB_PASSWORD}';

grant connect on database "${POSTGRES_DB}" to agent_app, studio_ro;
grant usage on schema public to agent_app, studio_ro;

-- Lecture seule pour le studio
grant select on all tables in schema public to studio_ro;

-- Agent : lecture/écriture sur les tables métier
grant select, insert, update, delete on all tables in schema public to agent_app;
-- ...mais le journal d'audit reste en ajout seul (les triggers l'alimentent)
revoke update, delete, truncate on audit_log from agent_app;
-- Les écritures validées sont déjà protégées par des triggers ; on retire en plus TRUNCATE
revoke truncate on all tables in schema public from agent_app;

grant usage, select on all sequences in schema public to agent_app;
grant execute on all functions in schema public to agent_app;
EOSQL

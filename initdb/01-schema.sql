-- =====================================================================
-- Agent comptable SYSCOHADA : schéma PostgreSQL (compatible Supabase)
-- Principes :
--   * l'IA PROPOSE, le code VÉRIFIE, l'humain VALIDE
--   * une écriture validée (posted) ne se modifie jamais : on la contre-passe
--   * tout changement important est journalisé (audit_log)
-- =====================================================================

create extension if not exists pgcrypto;

-- ---------- Types ----------
create type account_status   as enum ('active', 'pending', 'rejected');
create type entry_status     as enum ('draft', 'posted');
create type document_status  as enum ('received', 'needs_clarification', 'extracted', 'entry_proposed', 'booked', 'rejected');
create type rule_status      as enum ('proposed', 'validated', 'disabled');
create type request_status   as enum ('incomplete', 'submitted', 'approved', 'rejected', 'order_created', 'paid', 'justified', 'received', 'cancelled');
create type user_role        as enum ('accountant', 'manager', 'requester', 'admin');

-- ---------- Organisation / utilisateurs ----------
-- "workspace" = un tenant Lark (vous en avez deux). Même cerveau, plusieurs entrées.
create table workspaces (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  channel     text not null default 'lark',   -- lark, whatsapp, web...
  created_at  timestamptz not null default now()
);

create table users (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references workspaces(id),
  external_id   text not null,                 -- open_id Lark
  full_name     text not null,
  role          user_role not null default 'requester',
  active        boolean not null default true,
  created_at    timestamptz not null default now(),
  unique (workspace_id, external_id)
);

-- ---------- Plan comptable ----------
create table accounts (
  code          text primary key,              -- ex. '605'
  label         text not null,
  class         smallint generated always as (substring(code from 1 for 1)::smallint) stored,
  status        account_status not null default 'pending',
  proposed_by   text,                          -- 'agent' ou nom d'utilisateur
  validated_by  uuid references users(id),
  validated_at  timestamptz,
  created_at    timestamptz not null default now(),
  check (code ~ '^[1-9][0-9]*$')
);

create table journals (
  code   text primary key,                     -- AC, VE, BQ, CA, OD
  label  text not null
);

create table third_parties (
  id        uuid primary key default gen_random_uuid(),
  name      text not null,
  kind      text not null default 'supplier',  -- supplier / customer / employee
  tax_id    text,
  aux_account text,                            -- ex. 401xxx
  created_at timestamptz not null default now()
);

-- ---------- Documents (factures, reçus) ----------
create table documents (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid references workspaces(id),
  uploaded_by   uuid references users(id),
  storage_path  text not null,
  sha256        text not null,                 -- détecte les doublons de fichier
  status        document_status not null default 'received',
  quality_score numeric(3,2),                  -- 0..1, sous le seuil => demande de précision
  clarification text,                          -- question posée à l'utilisateur
  extracted     jsonb,                         -- fournisseur, date, n° facture, HT, TVA, TTC...
  invoice_ref   text,
  third_party_id uuid references third_parties(id),
  created_at    timestamptz not null default now(),
  unique (sha256)
);

-- ---------- Écritures ----------
create table entries (
  id            uuid primary key default gen_random_uuid(),
  journal_code  text not null references journals(code),
  entry_date    date not null,
  label         text not null,
  reference     text,
  document_id   uuid references documents(id),
  currency      char(3) not null default 'XOF',   -- XOF (UEMOA) ou XAF (CEMAC)
  status        entry_status not null default 'draft',
  proposed_by   text not null default 'agent',
  confidence    numeric(3,2),                  -- confiance de l'agent
  reversal_of   uuid references entries(id),   -- contre-passation
  posted_by     uuid references users(id),
  posted_at     timestamptz,
  tags          text[],                        -- étiquettes libres (max 5), proposées par l'IA (posées à la création)
  entities      jsonb,                          -- personnes/choses concernées (optionnel) : [{kind,label}]
  recorded_by   uuid references users(id),      -- qui a enregistré la dépense (posé à la création)
  created_at    timestamptz not null default now()
);

-- Suivi du justificatif d'une dépense enregistrée par texte. Table ANNEXE (mutable) : l'écriture, elle,
-- reste immuable une fois validée. due_at dépassé + justif_doc_id null => justif en retard.
create table expense_justifs (
  entry_id      uuid primary key references entries(id) on delete cascade,
  due_at        timestamptz not null,
  reminded_at   timestamptz,
  justif_doc_id uuid references documents(id),
  created_at    timestamptz not null default now()
);

-- Étiquettes déjà utilisées : réinjectées dans les prompts pour que l'IA reste cohérente.
create table known_tags (
  tag        text primary key,
  uses       int not null default 1,
  last_used  timestamptz not null default now()
);

create table entry_lines (
  id          uuid primary key default gen_random_uuid(),
  entry_id    uuid not null references entries(id) on delete cascade,
  account_code text not null references accounts(code),
  third_party_id uuid references third_parties(id),
  debit       numeric(18,2) not null default 0 check (debit  >= 0),
  credit      numeric(18,2) not null default 0 check (credit >= 0),
  memo        text,
  check ((debit = 0) <> (credit = 0))          -- une ligne = soit débit, soit crédit
);
create index on entry_lines (entry_id);
create index on entry_lines (account_code);
create index on entries (entry_date);

-- ---------- Règles apprises (validées par le comptable) ----------
-- C'est la "mémoire" de l'agent : explicite, traçable, réversible.
create table rules (
  id           uuid primary key default gen_random_uuid(),
  description  text not null,                  -- ex. "Factures CIE => compte 605"
  match_supplier text,                         -- motif fournisseur
  match_keywords text[],                       -- mots-clés dans la facture
  account_code text not null references accounts(code),
  status       rule_status not null default 'proposed',
  source_entry uuid references entries(id),    -- correction qui a déclenché la règle
  proposed_at  timestamptz not null default now(),
  validated_by uuid references users(id),
  validated_at timestamptz
);

-- ---------- Demandes de décaissement ----------
-- Cycle : incomplete (sans pièce) -> submitted (pièce fournie) -> approved -> order_created
--         -> paid (fonds remis) -> justified (justificatif rapproché). rejected/cancelled possibles.
create table disbursement_requests (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid references workspaces(id),
  requester_id  uuid references users(id),
  amount        numeric(18,2) not null check (amount > 0),
  currency      char(3) not null default 'XOF',
  purpose       text not null,
  beneficiary   text,
  requested_date date,                           -- date de décaissement souhaitée
  payment_method text not null default 'virement' check (payment_method in ('virement', 'especes', 'mobile_money')),
  status        request_status not null default 'incomplete',
  requires_double_approval boolean not null default false,  -- montant au-dessus du seuil
  approved_by   uuid references users(id),        -- validateur (comptable ou manager)
  approved_at   timestamptz,
  approved_by2  uuid references users(id),        -- (réservé) 2e validation éventuelle
  approved_at2  timestamptz,
  disbursed_by  uuid references users(id),        -- qui a remis le cash (comptable ou manager caissier)
  disbursed_at  timestamptz,
  received_at   timestamptz,                       -- (virement) réception en caisse confirmée
  rejected_reason text,
  external_order_ref text,                        -- id du virement dans l'Assistant Caisse
  order_pdf_path text,                            -- PDF de l'ordre de virement re-stocké localement
  entry_id      uuid references entries(id),      -- écriture d'avance (au paiement)
  settlement_entry_id uuid references entries(id),-- écriture de solde (au rapprochement du justificatif)
  funds_received_at timestamptz,                  -- fonds remis / virement constaté
  justif_due_at timestamptz,                      -- échéance du justificatif
  justified_amount numeric(18,2),                 -- montant justifié (rapprochement)
  remainder     numeric(18,2),                    -- >0 reliquat à restituer, <0 complément à rembourser
  created_at    timestamptz not null default now()
);
create index on disbursement_requests (status);
create index on disbursement_requests (requester_id);

-- Pièces d'une demande : justificatif a priori (devis/proforma/facture) puis justificatif a posteriori.
create table disbursement_documents (
  id          uuid primary key default gen_random_uuid(),
  request_id  uuid not null references disbursement_requests(id) on delete cascade,
  document_id uuid not null references documents(id),
  kind        text not null default 'piece' check (kind in ('piece', 'justificatif')),
  created_at  timestamptz not null default now()
);
create index on disbursement_documents (request_id);

-- Relances sur justificatif en retard (puis alerte comptable).
create table reminders (
  id          uuid primary key default gen_random_uuid(),
  request_id  uuid not null references disbursement_requests(id) on delete cascade,
  channel     text,
  attempt     int not null default 1,
  sent_at     timestamptz not null default now(),
  escalated   boolean not null default false      -- alerte comptable émise
);
create index on reminders (request_id);

-- ---------- Journal d'audit (append-only) ----------
create table audit_log (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  actor       text,
  table_name  text not null,
  row_id      text,
  action      text not null,
  old_data    jsonb,
  new_data    jsonb
);

create or replace function audit_row() returns trigger language plpgsql as $$
begin
  insert into audit_log(actor, table_name, row_id, action, old_data, new_data)
  values (
    coalesce(current_setting('app.actor', true), current_user),
    tg_table_name,
    coalesce((case when tg_op = 'DELETE' then to_jsonb(old) else to_jsonb(new) end)->>'id',
             (case when tg_op = 'DELETE' then to_jsonb(old) else to_jsonb(new) end)->>'code'),
    tg_op,
    case when tg_op in ('UPDATE','DELETE') then to_jsonb(old) end,
    case when tg_op in ('INSERT','UPDATE') then to_jsonb(new) end
  );
  return coalesce(new, old);
end $$;

create trigger audit_entries   after insert or update or delete on entries                for each row execute function audit_row();
create trigger audit_accounts  after insert or update or delete on accounts               for each row execute function audit_row();
create trigger audit_rules     after insert or update or delete on rules                  for each row execute function audit_row();
create trigger audit_requests  after insert or update or delete on disbursement_requests  for each row execute function audit_row();
create trigger audit_disb_docs after insert or update or delete on disbursement_documents for each row execute function audit_row();
create trigger audit_reminders after insert or update or delete on reminders               for each row execute function audit_row();

create or replace function audit_log_readonly() returns trigger language plpgsql as $$
begin raise exception 'audit_log est en ajout seul'; end $$;
create trigger audit_log_no_change before update or delete on audit_log
  for each row execute function audit_log_readonly();

-- ---------- Garde-fous comptables ----------
-- 1) Une écriture validée ne se modifie ni ne se supprime.
create or replace function entries_lock() returns trigger language plpgsql as $$
begin
  if old.status = 'posted' then
    raise exception 'Écriture validée : contre-passez au lieu de modifier (id %)', old.id;
  end if;
  if tg_op = 'DELETE' then return old; end if;
  -- passage draft -> posted : contrôles
  if new.status = 'posted' then
    if (select count(*) from entry_lines where entry_id = new.id) < 2 then
      raise exception 'Une écriture doit avoir au moins 2 lignes';
    end if;
    if (select coalesce(sum(debit),0) - coalesce(sum(credit),0) from entry_lines where entry_id = new.id) <> 0 then
      raise exception 'Écriture déséquilibrée (débit <> crédit)';
    end if;
    if exists (select 1 from entry_lines l join accounts a on a.code = l.account_code
               where l.entry_id = new.id and a.status <> 'active') then
      raise exception 'Écriture utilisant un compte non validé';
    end if;
    new.posted_at := now();
  end if;
  return new;
end $$;
create trigger entries_lock_upd before update on entries for each row execute function entries_lock();
create trigger entries_lock_del before delete on entries for each row execute function entries_lock();

-- 2) Les lignes d'une écriture validée sont figées.
create or replace function lines_lock() returns trigger language plpgsql as $$
declare eid uuid; st entry_status;
begin
  eid := coalesce(new.entry_id, old.entry_id);
  select status into st from entries where id = eid;
  if st = 'posted' then
    raise exception 'Lignes figées : l''écriture % est validée', eid;
  end if;
  return coalesce(new, old);
end $$;
create trigger lines_lock_trg before insert or update or delete on entry_lines
  for each row execute function lines_lock();

-- 3) Contre-passation : crée l'écriture inverse d'une écriture validée.
create or replace function reverse_entry(p_entry uuid, p_actor uuid default null)
returns uuid language plpgsql as $$
declare e entries%rowtype; new_id uuid;
begin
  select * into e from entries where id = p_entry and status = 'posted';
  if not found then raise exception 'Écriture % introuvable ou non validée', p_entry; end if;
  insert into entries(journal_code, entry_date, label, reference, document_id, currency, status, proposed_by, reversal_of)
  values (e.journal_code, current_date, 'Contre-passation : ' || e.label, e.reference, e.document_id, e.currency, 'draft', 'system', e.id)
  returning id into new_id;
  insert into entry_lines(entry_id, account_code, third_party_id, debit, credit, memo)
  select new_id, account_code, third_party_id, credit, debit, memo from entry_lines where entry_id = p_entry;
  update entries set status = 'posted', posted_by = p_actor where id = new_id;
  return new_id;
end $$;

-- ---------- Rapports (journalier / hebdomadaire) ----------
-- Dépenses (classe 6) et recettes (classe 7) validées.
create view v_report_daily as
select e.entry_date                                   as jour,
       sum(l.debit  - l.credit) filter (where a.class = 6) as depenses,
       sum(l.credit - l.debit)  filter (where a.class = 7) as recettes,
       count(distinct e.id)                           as nb_ecritures
from entries e
join entry_lines l on l.entry_id = e.id
join accounts a    on a.code = l.account_code
where e.status = 'posted'
group by e.entry_date;

create view v_report_weekly as
select date_trunc('week', e.entry_date)::date        as semaine,
       sum(l.debit  - l.credit) filter (where a.class = 6) as depenses,
       sum(l.credit - l.debit)  filter (where a.class = 7) as recettes,
       count(distinct e.id)                           as nb_ecritures
from entries e
join entry_lines l on l.entry_id = e.id
join accounts a    on a.code = l.account_code
where e.status = 'posted'
group by 1;

create view v_report_by_account as
select e.entry_date as jour, a.code, a.label,
       sum(l.debit - l.credit) as solde
from entries e
join entry_lines l on l.entry_id = e.id
join accounts a    on a.code = l.account_code
where e.status = 'posted' and a.class in (6, 7)
group by e.entry_date, a.code, a.label;

-- ---------- Données de départ : journaux ----------
insert into journals(code, label) values
  ('AC','Achats'), ('VE','Ventes'), ('BQ','Banque'), ('CA','Caisse'), ('OD','Opérations diverses');
-- Le plan comptable (codes Odoo) est importé par 03-plan-comptable.sql, activé par 04.

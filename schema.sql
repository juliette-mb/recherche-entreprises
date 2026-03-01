-- ============================================================
-- Deal Sourcing Platform — Schéma Supabase
-- ============================================================

-- ── Enum statuts ────────────────────────────────────────────
create type statut_vendeur as enum (
  'prospect',
  'contacté',
  'intéressé',
  'mandat signé'
);

create type statut_acheteur as enum (
  'prospect',
  'contacté',
  'intéressé',
  'signé'
);

-- ── Table vendeurs ───────────────────────────────────────────
create table vendeurs (
  id              uuid primary key default gen_random_uuid(),

  -- Entreprise
  nom_entreprise  text,
  siren           text,
  ca              bigint,
  resultat_net    bigint,
  secteur         text,
  adresse         text,
  site_web        text,
  lien_pappers    text,

  -- Dirigeant
  nom_dirigeant   text,
  age_dirigeant   integer,
  email           text,
  telephone       text,

  -- Deal
  statut          statut_vendeur not null default 'prospect',
  raison_cession  text,
  notes           text,

  -- Timestamps
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- ── Table acheteurs ──────────────────────────────────────────
create table acheteurs (
  id              uuid primary key default gen_random_uuid(),

  -- Contact
  nom             text,
  prenom          text,
  titre           text,
  entreprise      text,
  email           text,
  telephone       text,

  -- Profil M&A
  secteurs_interet  text,
  taille_cibles     text,
  notes             text,

  -- Deal
  statut          statut_acheteur not null default 'prospect',

  -- Timestamps
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- ── Trigger updated_at automatique ──────────────────────────
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger vendeurs_updated_at
  before update on vendeurs
  for each row execute function set_updated_at();

create trigger acheteurs_updated_at
  before update on acheteurs
  for each row execute function set_updated_at();

-- ── Index utiles ─────────────────────────────────────────────
create index on vendeurs (statut);
create index on vendeurs (siren);
create index on vendeurs (created_at desc);

create index on acheteurs (statut);
create index on acheteurs (created_at desc);

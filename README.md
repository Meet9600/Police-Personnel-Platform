# Gujarat Police Management & AI Platform

An internal, **fully on-premise** personnel management and decision-support system
for a Gujarat Police district (SP office). It cleans a messy bilingual
(Gujarati/English) personnel database into an analytical store, then provides:

1. **Team Recommendation** — specify a case's required skills, team size, and rank
   mix; get the most suitable team from the relevant station, with a transparent,
   auditable reason for every choice.
2. **Local NLP Q&A** — ask plain-language questions (English or Gujarati) about
   personnel; answers are grounded in the database via safe query templates.
3. **Bilingual UI** — every screen works in English and ગુજરાતી.

> **Privacy:** Everything runs locally. No external API, cloud service, or network
> egress is used for any data, ML, or NLP processing. PII columns (Aadhaar, PAN,
> mobile, addresses, photo) never enter the analytical store or the AI layer.

---

## Architecture

```
SOURCE DB (public schema, READ-ONLY, 40 tables)
   │  ETL pipeline (normalize Gujarati, resolve identity, flag missing)
   ▼
CLEAN STORE (clean schema): person · posting_history · specialization ·
             performance · station_capacity · vw_ml_features
   ├── Recommendation engine (explainable scoring)
   ├── NLP Q&A (grounded text-to-SQL, read-only role)
   └── Flask web app (bilingual UI + session auth)
```

## Prerequisites

- PostgreSQL 17
- Python 3.10+
- `pip install -r requirements.txt`

## Setup

```bash
# 1. Environment (point at your real DB)
export PGHOST=/var/run/postgresql PGPORT=5432
export PGDATABASE=police_management PGUSER=app_user PGPASSWORD=...
export SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"

# 2. Build the clean analytical store
psql -d "$PGDATABASE" -f pipeline/01_clean_schema.sql
psql -d "$PGDATABASE" -f pipeline/02_reference_data.sql

# 3. Create a read-only role for the NLP executor (defense in depth)
psql -d "$PGDATABASE" <<'SQL'
CREATE ROLE nlp_readonly LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE police_management TO nlp_readonly;
GRANT USAGE ON SCHEMA clean TO nlp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA clean TO nlp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA clean GRANT SELECT ON TABLES TO nlp_readonly;
SQL
export PG_RO_USER=nlp_readonly PG_RO_PASSWORD='change-me'

# 4. Run the ETL pipeline (incremental + idempotent; safe to re-run)
python3 pipeline/run_pipeline.py

# 5. Compile UI translations
pybabel compile -d app/translations

# 6. Start the app (use a production WSGI server for real deployment)
python3 -m flask --app app.main run --port 5000
#   production: gunicorn -w 4 -b 127.0.0.1:5000 app.main:app
```

Open http://127.0.0.1:5000 and sign in. Real accounts authenticate against the
existing `public.users` table (hashed passwords). A demo login (`admin` /
`$DEMO_PASS`) is available until real users are wired; disable it in production.

## Running the pipeline on a schedule (new recruits & transfers)

The pipeline is **incremental, idempotent, and transfer-aware**:
- A new source id → appends a new person.
- An existing id with a changed station (a transfer) → updates the person's
  current posting and appends a posting-history row; **never duplicates** them.

Run it via cron, e.g. nightly:
```
0 2 * * *  cd /opt/police-ai && PG...=... python3 pipeline/run_pipeline.py
```

## Tests

```bash
PYTHONPATH=. python3 tests/test_app.py     # 20 end-to-end checks
```
Covers auth, all pages, the language toggle, recommendation (logic + HTTP),
NLP grounding + nonsense rejection, and PII isolation. Exits non-zero on failure.

## Project layout

```
app/
  config.py        DB connectivity (RW + read-only roles)
  main.py          Flask routes, auth, i18n
  recommender.py   explainable team-recommendation engine
  templates/       bilingual Jinja templates (Tailwind)
  translations/    en (default) + gu catalogs
nlp/
  nlq_engine.py    grounded intent->SQL Q&A (offline, safe templates)
pipeline/
  01_clean_schema.sql   clean analytical store DDL (PG17)
  02_reference_data.sql ranks, specializations, 77-duty mapping
  run_pipeline.py       ETL: public -> clean
tests/
  test_app.py      end-to-end test suite
docs/
  DATA_DICTIONARY.md    source schema + verified data quirks
```

## Key data findings baked into the design

- Officers & employees are two families with overlapping ids → namespaced
  `O-<id>` / `E-<id>`.
- `appointment_date` is a data-entry timestamp, not a joining date → seniority is
  derived from `batch` (joining year). `batch` is absent for ~94% of staff, so
  experience is an optional weighted signal, never a hard filter.
- `services_periods.duties_performed` holds place names, not duties →
  specialization comes from the duties tables; tenure from service periods.
- `designation` is already clean; the Gujarati/English mixing is in names,
  study, batch, and duty descriptions — which is where normalization focuses.

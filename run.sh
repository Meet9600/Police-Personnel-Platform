#!/usr/bin/env bash
set -e

echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "Setting up environment variables..."
if [ ! -f .env ]; then
  cp .env.example .env
  # Generate a real secret key
  NEW_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
  sed -i '' "s/generate-a-long-random-hex-string/$NEW_SECRET/g" .env
  echo "✅ Created .env file with a generated secret key."
  echo "⚠️  Please double check .env if your PostgreSQL connection requires a specific PGUSER or PGPASSWORD."
fi

set -a
source .env
set +a

# Adjust default PGHOST if it's the mac default (/tmp)
export PGHOST=${PGHOST:-/tmp}

echo "Ensuring database '$PGDATABASE' exists..."
createdb "$PGDATABASE" 2>/dev/null || echo "Database might already exist or creation requires elevated privileges (continuing...)"

echo "Building clean analytical store schemas..."
psql -d "$PGDATABASE" -f pipeline/01_clean_schema.sql
psql -d "$PGDATABASE" -f pipeline/02_reference_data.sql

echo "Setting up NLP read-only role..."
psql -d "$PGDATABASE" <<'SQL'
DO
$do$
BEGIN
   IF NOT EXISTS (
      SELECT FROM pg_catalog.pg_roles
      WHERE  rolname = 'nlp_readonly') THEN
      CREATE ROLE nlp_readonly LOGIN PASSWORD 'change-me';
   END IF;
END
$do$;
GRANT CONNECT ON DATABASE police_management TO nlp_readonly;
GRANT USAGE ON SCHEMA clean TO nlp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA clean TO nlp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA clean GRANT SELECT ON TABLES TO nlp_readonly;
SQL

echo "Running ETL pipeline..."
python3 pipeline/run_pipeline.py

echo "Compiling UI translations..."
pybabel compile -d app/translations

echo "🚀 Starting Flask app on port 5000..."
python3 -m flask --app app.main run --port 5000

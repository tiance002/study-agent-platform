#!/usr/bin/env bash
set -euo pipefail
umask 077

root=/opt/study-plan/current
secret_state=/etc/study-plan/bootstrap-secrets.env

install -d -o root -g root -m 750 /etc/study-plan
install -d -o studyplan -g studyplan -m 750 /var/lib/study-plan

if [[ -e "$root/var" && ! -L "$root/var" ]]; then
  echo "$root/var exists but is not the required runtime symlink" >&2
  exit 1
fi
ln -sfn /var/lib/study-plan "$root/var"

if [[ ! -f "$secret_state" ]]; then
  app_db_password=$(openssl rand -hex 32)
  worker_db_password=$(openssl rand -hex 32)
  migration_db_password=$(openssl rand -hex 32)
  session_secret=$(openssl rand -hex 48)
  cookie_secret=$(openssl rand -hex 48)
  token_secret=$(openssl rand -hex 48)
  cat >"$secret_state" <<EOF
APP_DB_PASSWORD=$app_db_password
WORKER_DB_PASSWORD=$worker_db_password
MIGRATION_DB_PASSWORD=$migration_db_password
SESSION_SECRET=$session_secret
COOKIE_SECRET=$cookie_secret
TOKEN_SECRET=$token_secret
EOF
fi

# shellcheck disable=SC1090
source "$secret_state"

runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres \
  -v app_password="$APP_DB_PASSWORD" \
  -v worker_password="$WORKER_DB_PASSWORD" \
  -f "$root/scripts/sql/create_app_role.sql"

if ! runuser -u postgres -- psql -d postgres -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname = 'study_migrator'" | grep -qx 1; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres \
    -v migration_password="$MIGRATION_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE study_migrator LOGIN PASSWORD %L SUPERUSER',
    :'migration_password'
)
\gexec
SQL
fi

if ! runuser -u postgres -- psql -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'study_platform'" | grep -qx 1; then
  runuser -u postgres -- createdb -O study_migrator study_platform
fi

cat >/etc/study-plan/study-plan.env <<EOF
STUDY_PLATFORM_ENV=production
STUDY_PLATFORM_DSN=postgresql://study_app:$APP_DB_PASSWORD@127.0.0.1:5432/study_platform
STUDY_PLATFORM_WORKER_DSN=postgresql://study_worker:$WORKER_DB_PASSWORD@127.0.0.1:5432/study_platform
STUDY_PLATFORM_SESSION_SECRET=$SESSION_SECRET
STUDY_PLATFORM_COOKIE_SECRET=$COOKIE_SECRET
STUDY_PLATFORM_TOKEN_SECRET=$TOKEN_SECRET
STUDY_PLATFORM_COOKIE_SECURE=1
STUDY_PLATFORM_TRUSTED_ORIGINS=https://120.55.115.162
STUDY_PLATFORM_BEHIND_PROXY=1
STUDY_PLATFORM_TRUSTED_PROXIES=127.0.0.1
STUDY_PLATFORM_WEB_WORKERS=1
STUDY_PLATFORM_SESSION_TTL_MINUTES=480
STUDY_PLATFORM_AUTH_ATTEMPT_LIMIT=20
STUDY_PLATFORM_AUTH_ATTEMPT_WINDOW_SECONDS=600
STUDY_PLATFORM_TEACHING_MAX_INPUT_TOKENS=8000
STUDY_PLATFORM_TEACHING_MAX_OUTPUT_TOKENS=2000
STUDY_PLATFORM_TEACHING_PROJECT_BUDGET_MICRO=5000000
EOF

cat >/etc/study-plan/admin.env <<EOF
STUDY_PLATFORM_MIGRATION_DSN=postgresql+psycopg://study_migrator:$MIGRATION_DB_PASSWORD@127.0.0.1:5432/study_platform
STUDY_PLATFORM_BACKUP_DSN=postgresql://study_migrator:$MIGRATION_DB_PASSWORD@127.0.0.1:5432/study_platform
STUDY_PLATFORM_DSN=postgresql://study_app:$APP_DB_PASSWORD@127.0.0.1:5432/study_platform
EOF

if [[ ! -f /etc/study-plan/provider.env ]]; then
  cat >/etc/study-plan/provider.env <<'EOF'
STUDY_PLATFORM_TEACHING_PROVIDER=disabled
STUDY_PLATFORM_TEACHING_MODEL=deepseek-flash
STUDY_PLATFORM_TEACHING_BASE_URL=https://api.deepseek.com
STUDY_PLATFORM_TEACHING_API_KEY=
EOF
fi

chown root:studyplan /etc/study-plan/study-plan.env /etc/study-plan/provider.env
chmod 640 /etc/study-plan/study-plan.env /etc/study-plan/provider.env
chown root:root "$secret_state" /etc/study-plan/admin.env
chmod 600 "$secret_state" /etc/study-plan/admin.env

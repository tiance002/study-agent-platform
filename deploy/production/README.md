# Study Plan production deployment

This directory contains the first-release deployment assets for a single ECS
instance running Ubuntu 24.04, PostgreSQL, systemd, and nginx.

## Current ECS

- URL: `https://120.55.115.162`
- Application root: `/opt/study-plan/current`
- Environment files: `/etc/study-plan/`
- Database migrations: `0011`
- Web service: `study-plan-web.service`
- Ingestion worker: `study-plan-ingestion.service`
- Teaching worker: `study-plan-teaching.service`

The application and ingestion worker are enabled. The teaching provider is
deliberately disabled until a real API key is supplied. This preserves the
production fail-closed behavior and prevents accidental paid model calls.

## Fill the DeepSeek key

Do this directly on the server over SSH. The key must never be committed to
Git, copied into the repository, or pasted into a chat message.

```sh
sudoedit /etc/study-plan/provider.env
```

Set these values, replacing only the final value with the real key:

```dotenv
STUDY_PLATFORM_TEACHING_PROVIDER=openai
STUDY_PLATFORM_TEACHING_MODEL=deepseek-flash
STUDY_PLATFORM_TEACHING_BASE_URL=https://api.deepseek.com
STUDY_PLATFORM_TEACHING_API_KEY=PASTE_THE_REAL_KEY_HERE
```

The provider adapter uses the OpenAI-compatible DeepSeek Responses API. The
current DeepSeek model identifier is `deepseek-flash`; the old
`deepseek-v4-flash` name is not used in production configuration.

After saving the file:

```sh
sudo systemctl restart study-plan-web.service
sudo systemctl enable --now study-plan-teaching.service
sudo systemctl --no-pager --full status study-plan-web.service study-plan-teaching.service
```

The service account can read the provider file, but it cannot write it. The
file should remain owned by `root:studyplan` with mode `0640`.

## Create a first invitation

Run this on the server after the teaching provider is configured. The command
prints one single-use invitation token; do not store it in the repository.

```sh
set -a
. /etc/study-plan/admin.env
set +a
/opt/study-plan/venv/bin/python /opt/study-plan/current/tools/issue_invitation.py \
  --tenant t_first_release \
  --principal u_first_release
```

Open `https://120.55.115.162`, exchange the token once, and create a project.

## Operations

```sh
sudo systemctl --no-pager status study-plan-web.service study-plan-ingestion.service study-plan-teaching.service
sudo journalctl -u study-plan-web.service -u study-plan-ingestion.service -u study-plan-teaching.service -n 100 --no-pager
sudo systemctl list-timers --all 'study-plan-*'
```

Backups are written to `/var/backups/study-plan/`. The backup timer and the
restore check have been verified on the ECS. Do not restore over the live
database without a separate maintenance decision.

## Release and rollback

Each release is stored below `/opt/study-plan/releases/` and `current` is a
symlink. A rollback is a symlink switch followed by service restarts:

```sh
sudo ln -sfn /opt/study-plan/releases/<known-good-release> /opt/study-plan/current
sudo systemctl restart study-plan-web.service study-plan-ingestion.service study-plan-teaching.service
```

Run the migration and browser smoke checks before switching a release. Keep
the database migration level compatible with the selected application code.

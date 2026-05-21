# M365 Audit Ingestor

Headless service that pulls Microsoft 365 audit events (directory changes, sign-ins, provisioning, SharePoint / Exchange / Teams / AAD workload activity) and writes them into your `z_audit_logs_efk` / `z_audit_logs_efk_runs` tables in MariaDB. Ships with an optional admin dashboard for one-time tenant authorization, schema discovery, field mapping, dry-runs, and run review.

The full architecture, milestones, and pass criteria are in [`PLAN.md`](./PLAN.md). This README covers **how to deploy and operate** the service.

---

## Prerequisites

### On the host

| Requirement | Version | Notes |
|---|---|---|
| Linux host | any modern | tested on Ubuntu 22.04 / Debian 12 |
| Docker Engine | ≥ 24 | with the `compose` plugin (`docker compose version`) |
| `openssl` | any | used for secret generation |
| `python3` | ≥ 3.10 | used by `deploy.sh` for password hashing only — the app itself runs in the container |
| `curl` | any | readiness probe |
| Outbound HTTPS to `login.microsoftonline.com`, `graph.microsoft.com`, `manage.office.com` | — | corp proxies must allow these |
| Free TCP port | default 8080 | dashboard port; configurable in `deploy.sh` |

### In MariaDB

You can either:

- **Bring your own** MariaDB 10.5+ / 11.x reachable from the host, with a user that has `CREATE, ALTER, INDEX, INSERT, SELECT, UPDATE, DELETE` on a target database, **or**
- Let `deploy.sh` start a local MariaDB 11.4 inside the compose stack (`with-mariadb` profile). Data persists in a named volume.

The app **will not migrate** an existing `z_audit_logs_efk*` schema. If the tables already exist with the column / key definitions in `PLAN.md §7`, they're used as-is. If they don't exist, the app creates them from the DDL you provided. If they exist but mismatch, the app refuses to start and prints a diff.

### In Microsoft 365 / Entra ID

> **Sovereign clouds.** The default is `commercial`. For `gcc-high`, `dod`, or `china` tenants, set `AZURE_CLOUD` accordingly during `deploy.sh`; endpoint URLs are switched automatically (see `PLAN.md §4.4`). The app refuses to start on any unrecognized value — no silent fallback.

You need an **Entra app registration** in the target tenant. The app reg needs:

**Microsoft Graph — Application permissions** (admin consent required):

- `AuditLog.Read.All`
- `Directory.Read.All`
- `User.Read.All`

**Office 365 Management APIs — Application permissions** (admin consent required):

- `ActivityFeed.Read`
- `ActivityFeed.ReadDlp` *(optional — only if you want DLP records)*
- `ServiceHealth.Read` *(optional)*

You also need:

- The tenant ID (GUID or `contoso.onmicrosoft.com`).
- The app's client ID.
- A client secret (or a certificate — certificate auth lands post-v1).
- A redirect URI registered on the app reg: `http://<your-host>:<WEB_PORT>/m365/callback` (used **only** during the one-time admin-consent click in the dashboard).
- An E3/E5 (or equivalent) license SKU on the tenant for the Office 365 Management Activity API to expose `Audit.*` content. Without it, only the Graph half of the ingestor works; the Mgmt API half will log a 403 and degrade.

---

## Deploy

```bash
git clone <this-repo> m365ai && cd m365ai
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` is interactive and idempotent. On first run it asks for:

1. **Dashboard credentials.** Username + password (≥ 12 chars). Password is hashed with PBKDF2 in `.env` for bootstrap; the app re-hashes with Argon2id on first login.
2. **Timezone.** Detected from the host (`timedatectl`). Used as the container `TZ` env. Defaults to `UTC` if detection fails.
3. **Target MariaDB.** Either point at your existing database or let compose start one. Connection string is built at runtime from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS`.
4. **Azure tenant / client ID / client secret.** Stored in `.env` mode `600`.
5. **Runtime knobs.** Poll interval (default `300 s`), first-run Graph lookback (default `24 h`), Mgmt API content types (default: SharePoint + Exchange + AAD + General), dashboard host port (default `8080`).

After answering, the script:

- Writes `./.env` atomically with mode `600`.
- Builds the image (`docker compose build`).
- Brings the stack up (`docker compose up -d`, plus `--profile with-mariadb` if you chose the bundled DB).
- Polls `/readyz` for up to 60 s and prints the dashboard URL when ready.

Re-running `deploy.sh` only re-prompts for values that are missing from `.env` — to rotate the dashboard password, the DB password, or the Azure client secret, delete that single key from `.env` before re-running.

---

## Production deployment hardening

`deploy.sh` brings the stack up bound to `127.0.0.1:8080` deliberately. Before exposing the dashboard, do these three things:

1. **Terminate TLS at a reverse proxy.** The compose project ships a `docker-compose.proxy.yml` overlay with Caddy and a sample `Caddyfile`. Run:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d
   ```
   Caddy handles ACME automatically. The app itself stays HTTP-only behind the proxy. Full pattern (incl. Traefik / nginx equivalents) is in `PLAN.md §10.3`.

2. **Enable OIDC SSO.** Add to `.env`:
   ```dotenv
   OIDC_ISSUER_URL=https://login.example.com
   OIDC_CLIENT_ID=...
   OIDC_CLIENT_SECRET=...
   OIDC_REDIRECT_URI=https://audit.example.com/auth/oidc/callback
   OIDC_REQUIRED_GROUP=audit-admins   # optional
   ```
   `kill -HUP web`. The local login form disappears; `/login` redirects to the IdP. The bootstrap `DASHBOARD_USER` becomes a break-glass account, usable only when you flip `LOCAL_LOGIN_ENABLED=true`. Details in `PLAN.md §9.2`.

3. **Confirm the container is hardened.**
   ```bash
   docker compose exec web id                       # uid=10001(app)
   docker inspect m365ai-web | grep -i readonly      # ReadonlyRootfs: true
   docker inspect m365ai-web | grep -i CapDrop       # CapDrop: ["ALL"]
   ```
   All three must hold. They're set in the Dockerfile and compose by default (`PLAN.md §10.1`–`§10.2`); the checks exist to catch override mistakes.

## First-run inside the dashboard

Open `http://<host>:8080/`, log in, then:

1. **Config** → click **Test DB**. Must turn green. (If not, fix `DB_*` in `.env` and re-run `deploy.sh`.)
2. **M365** → click **Authorize tenant**. You'll be bounced to `login.microsoftonline.com` to grant admin consent for the permissions listed above. After consent, the page shows ✅ for each scope.
3. **Discover** → pick a feed (e.g. `graph.directoryAudits`), pull 50 sample events. Inspect the inferred schema.
4. **Mapping** → confirm or adjust the seeded mapping rules. Click **Dry-run** to see how a sampled event would land in `z_audit_logs_efk` *without* writing anything.
5. The worker is already polling on its schedule. Go to **Runs** to confirm rows are being inserted; `source` will be `Microsoft365`, `instance` will distinguish workloads.

---

## Operating

| Action | How |
|---|---|
| Tail logs | `docker compose logs -f --tail=200 worker web` |
| Restart after `.env` edit (without re-deploy) | `docker compose kill -s HUP worker web` |
| Stop everything | `docker compose down` (keeps DB volume) |
| Wipe local DB (only with bundled MariaDB) | `docker compose down -v` |
| Trigger a manual backfill | from `/runs` page → **Backfill** button, or `docker compose exec worker python -m app.cli backfill --since 2026-04-01 --feeds directoryAudits,Audit.SharePoint` |
| Rotate a secret | delete the key from `.env`, re-run `./deploy.sh` |
| Health check (for an external monitor) | `curl -fsS http://localhost:8080/readyz` returns `200` only if Graph + Mgmt tokens are fresh and DB ping OK |
| Review admin actions | dashboard `/admin-events` — read-only log of logins, mapping edits, backfills, secret rotations, consent grants (`PLAN.md §3.7`) |
| Reproduce a canonicalized row's mapping rule | `SELECT * FROM z_m365ai_mapping_rules WHERE subsource=? AND target_column=? AND source_jsonpath=? AND valid_from <= ? AND COALESCE(valid_to,'9999-12-31') > ?;` (`PLAN.md §3.6`) |

---

## Headless-only mode

If you don't want the dashboard exposed at all, edit `docker-compose.yml` and comment out the `web` service (or scale it to 0: `docker compose up -d --scale web=0`). Worker keeps running. You lose the discovery / mapping UI but ingestion is unaffected — defaults from `app/mappings/default_*.yml` are used as-is.

---

## Troubleshooting

For anything beyond the quick table below, the **Operational Runbook** in [`PLAN.md §12`](./PLAN.md) has full Symptom → Detection → Diagnosis → Recovery → Prevention playbooks for the 11 most likely failure modes (stuck cursors, expired secrets, schema drift, disabled Mgmt subscriptions, partial-day failures, replay/backfill, pool exhaustion, 429 storms, disk fill, mid-run crashes, and DR).

| Symptom | Likely cause | Fix | Full playbook |
|---|---|---|---|
| `deploy.sh` aborts: `missing required binary: docker` | Docker not installed or not in PATH | Install Docker Engine + Compose plugin | — |
| Stack doesn't become ready in 60 s | Container can't reach DB | `docker compose logs db worker`; check `DB_HOST` is reachable *from inside the container* (use `host.docker.internal` on Docker Desktop) | — |
| Worker logs `403 Forbidden` on Mgmt API subscription start | Tenant SKU lacks unified audit log, or admin consent not granted | Verify license; revisit `/m365` and click **Authorize tenant** again | §12.4 |
| App refuses to start with `expected column dedup_hash` | Pre-existing `z_audit_logs_efk` doesn't match the schema | Align the existing table to the contract, or drop it and let the app recreate it | §12.3 |
| `429 Too Many Requests` repeating in logs | Tenant throttling tight | Increase `POLL_INTERVAL_S` in `.env`, `kill -HUP` the worker | §12.8 |
| `AADSTSxxxx` on token refresh; `/readyz` flips to 503 | Client secret expired or rotated | Rotate secret, delete `AZURE_CLIENT_SECRET` from `.env`, re-run `deploy.sh` | §12.2 |
| `cursor_lag_seconds` rising, no new rows for a feed | Cursor invalidated, token broken, or upstream incident | Triage by HTTP code in worker logs | §12.1 |
| Dashboard `/mapping` coverage gauge red (< 90 %) | New M365 event types not yet canonicalized | Promote top unmapped values via the UI | `PLAN.md §3.4.3` |
| Dashboard login takes ~2 s | First login re-hashes bootstrap PBKDF2 → Argon2id | One-time only | — |

---

## Files in this repo

| Path | Purpose |
|---|---|
| `PLAN.md` | Authoritative technical plan, milestones, pass criteria |
| `deploy.sh` | Interactive deployer (this README's `Deploy` section) |
| `.gitignore` | Excludes secrets, runtime data, Python caches |
| `README.md` | You are here |
| `Dockerfile`, `docker-compose.yml`, `Makefile`, `app/…`, `tests/…` | Created during implementation per `PLAN.md §13` |

---

## License & support

Internal tool. No external support channel.

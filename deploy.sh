#!/usr/bin/env bash
# deploy.sh — first-run bootstrap for the M365 Audit Ingestor.
# Interactive, idempotent. Writes ./.env (mode 600) and brings the stack up.
# Re-running asks only for the values it can't find.

set -euo pipefail

# ---------------------------------------------------------------------------
# Karpathy: surface assumptions. This script assumes:
#   - bash 4+, docker (with compose plugin), openssl, python3
#   - the current user can talk to the docker daemon
#   - we're in the repo root next to docker-compose.yml
# Anything else is fatal and surfaced loudly.
# ---------------------------------------------------------------------------

err()  { printf '\033[31m[ERR ]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
info() { printf '\033[36m[INFO]\033[0m %s\n' "$*"; }
ask()  { local prompt="$1" default="${2-}" var
        if [[ -n "$default" ]]; then read -rp "$prompt [$default]: " var; echo "${var:-$default}"
        else read -rp "$prompt: " var; echo "$var"; fi; }
ask_secret() { local prompt="$1" var
        read -rsp "$prompt: " var; echo >&2; echo "$var"; }

ENV_FILE="$(pwd)/.env"
COMPOSE_FILE="$(pwd)/docker-compose.yml"

# --- Preflight ---------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { err "missing required binary: $1"; exit 1; }; }
need docker
need openssl
need python3
docker compose version >/dev/null 2>&1 || { err "docker compose plugin not found"; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { err "docker-compose.yml not found in $(pwd)"; exit 1; }

# --- Load existing .env (so re-runs don't re-ask) ----------------------------
declare -A CFG=()
if [[ -f "$ENV_FILE" ]]; then
  info "found existing .env — will only ask for missing values"
  # shellcheck disable=SC1090
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    CFG["$k"]="$v"
  done < "$ENV_FILE"
fi

get() { echo "${CFG[$1]:-${2-}}"; }
set_kv() { CFG["$1"]="$2"; }

# --- 1. Dashboard credentials ------------------------------------------------
info "step 1/5: dashboard admin credentials"
DASH_USER=$(ask "Dashboard username" "$(get DASHBOARD_USER admin)")
if [[ -z "$(get DASHBOARD_PASS_HASH)" ]]; then
  while :; do
    pw1=$(ask_secret "Dashboard password (min 12 chars)")
    pw2=$(ask_secret "Confirm password")
    [[ "$pw1" == "$pw2" ]] || { err "passwords do not match"; continue; }
    [[ ${#pw1} -ge 12 ]] || { err "password too short"; continue; }
    break
  done
  # argon2id via python -- avoids pulling in another binary
  DASH_HASH=$(python3 - "$pw1" <<'PY'
import sys, os, base64, hashlib
# bootstrap-only: PBKDF2 here; the app re-hashes with argon2id on first login.
# Keeping deploy.sh dependency-free is worth this trade.
pw = sys.argv[1].encode()
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha256', pw, salt, 200_000)
print("pbkdf2$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode())
PY
)
  set_kv DASHBOARD_USER "$DASH_USER"
  set_kv DASHBOARD_PASS_HASH "$DASH_HASH"
else
  set_kv DASHBOARD_USER "$DASH_USER"
  info "keeping existing dashboard password hash (delete DASHBOARD_PASS_HASH from .env to rotate)"
fi

# --- 2. Timezone -------------------------------------------------------------
info "step 2/5: timezone"
host_tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
TZ_VAL=$(ask "Container TZ" "$(get TZ "$host_tz")")
set_kv TZ "$TZ_VAL"

# --- 3. Database -------------------------------------------------------------
info "step 3/5: target MariaDB"
echo "  1) use an existing MariaDB you already host"
echo "  2) let me start a local MariaDB in docker-compose (profile: with-mariadb)"
db_choice=$(ask "Choose 1 or 2" "$(get DB_MODE 1)")
set_kv DB_MODE "$db_choice"

if [[ "$db_choice" == "2" ]]; then
  set_kv COMPOSE_PROFILES "with-mariadb"
  DB_HOST=$(ask "DB host (as seen from app container)" "$(get DB_HOST db)")
  DB_PORT=$(ask "DB port"   "$(get DB_PORT 3306)")
  DB_NAME=$(ask "DB name"   "$(get DB_NAME m365_audit)")
  DB_USER=$(ask "DB user"   "$(get DB_USER m365)")
  if [[ -z "$(get DB_PASS)" ]]; then
    DB_PASS=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
    info "generated DB password"
  else
    DB_PASS=$(get DB_PASS)
  fi
  set_kv MARIADB_ROOT_PASSWORD "$(get MARIADB_ROOT_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)")"
else
  DB_HOST=$(ask "DB host" "$(get DB_HOST)")
  DB_PORT=$(ask "DB port" "$(get DB_PORT 3306)")
  DB_NAME=$(ask "DB name" "$(get DB_NAME)")
  DB_USER=$(ask "DB user" "$(get DB_USER)")
  if [[ -z "$(get DB_PASS)" ]]; then
    DB_PASS=$(ask_secret "DB password")
  else
    DB_PASS=$(get DB_PASS)
    info "keeping existing DB password (delete DB_PASS from .env to change)"
  fi
fi
set_kv DB_HOST "$DB_HOST"; set_kv DB_PORT "$DB_PORT"
set_kv DB_NAME "$DB_NAME"; set_kv DB_USER "$DB_USER"; set_kv DB_PASS "$DB_PASS"
# DSN built at runtime by the app from the parts above.

# --- 4. Azure / M365 app registration ---------------------------------------
info "step 4/5: Azure app registration (Microsoft Graph + Mgmt API)"
echo "    see README.md §Prerequisites for the exact permissions to grant."

# 4a. Sovereign cloud selection (D-14 / PLAN §4.4)
echo "    target cloud: commercial | gcc-high | dod | china"
while :; do
  AZ_CLOUD=$(ask "AZURE_CLOUD" "$(get AZURE_CLOUD commercial)")
  case "$AZ_CLOUD" in
    commercial|gcc-high|dod|china) break ;;
    *) err "must be one of: commercial, gcc-high, dod, china" ;;
  esac
done
set_kv AZURE_CLOUD "$AZ_CLOUD"

AZ_TENANT=$(ask "AZURE_TENANT_ID (GUID or domain)" "$(get AZURE_TENANT_ID)")
AZ_CLIENT=$(ask "AZURE_CLIENT_ID (app reg)"        "$(get AZURE_CLIENT_ID)")
if [[ -z "$(get AZURE_CLIENT_SECRET)" ]]; then
  AZ_SECRET=$(ask_secret "AZURE_CLIENT_SECRET")
else
  AZ_SECRET=$(get AZURE_CLIENT_SECRET)
  info "keeping existing client secret (delete AZURE_CLIENT_SECRET from .env to rotate)"
fi
set_kv AZURE_TENANT_ID "$AZ_TENANT"
set_kv AZURE_CLIENT_ID "$AZ_CLIENT"
set_kv AZURE_CLIENT_SECRET "$AZ_SECRET"

# --- 5. Runtime tuning -------------------------------------------------------
info "step 5/5: runtime tuning (sane defaults — press enter)"
set_kv POLL_INTERVAL_S "$(ask 'Poll interval (seconds)' "$(get POLL_INTERVAL_S 300)")"
set_kv GRAPH_LOOKBACK_HOURS "$(ask 'First-run Graph lookback (hours)' "$(get GRAPH_LOOKBACK_HOURS 24)")"
set_kv MGMT_CONTENT_TYPES "$(ask 'Mgmt API content types (csv)' "$(get MGMT_CONTENT_TYPES 'Audit.SharePoint,Audit.Exchange,Audit.AzureActiveDirectory,Audit.General')")"
set_kv WEB_PORT          "$(ask 'Dashboard port (host)' "$(get WEB_PORT 8080)")"
set_kv APP_SECRET_KEY    "$(get APP_SECRET_KEY "$(openssl rand -hex 32)")"

# --- Write .env atomically, mode 600 ----------------------------------------
tmp=$(mktemp ./.env.XXXXXX)
{
  echo "# generated by deploy.sh on $(date -Iseconds)"
  echo "# DO NOT COMMIT THIS FILE."
  for k in "${!CFG[@]}"; do printf '%s=%s\n' "$k" "${CFG[$k]}"; done | sort
} > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
ok ".env written (mode 600)"

# --- Bring the stack up ------------------------------------------------------
info "building image"
docker compose build

info "starting stack"
if [[ "$(get DB_MODE)" == "2" ]]; then
  docker compose --profile with-mariadb up -d
else
  docker compose up -d
fi

# --- Wait for /readyz --------------------------------------------------------
info "waiting for /readyz (up to 60 s)"
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:$(get WEB_PORT)/readyz" >/dev/null 2>&1; then
    ok "dashboard ready at http://localhost:$(get WEB_PORT)/  (user: $(get DASHBOARD_USER))"
    info "next step: open the dashboard → /m365 → click 'Authorize tenant' to grant admin consent"
    echo
    info "PRODUCTION CHECKLIST (not enforced by this script):"
    echo "  • TLS:    terminate at a reverse proxy (Caddy/Traefik) — see PLAN.md §10.3"
    echo "           the app binds 127.0.0.1:${CFG[WEB_PORT]} on purpose; do not expose it publicly."
    echo "  • SSO:    add OIDC_ISSUER_URL + OIDC_CLIENT_ID/SECRET to .env to disable local login"
    echo "           and route /login through your IdP — see PLAN.md §9.2"
    echo "  • Audit:  /admin-events page logs every state-changing action — review it after first use"
    exit 0
  fi
  sleep 2
done

err "stack did not become ready in 60 s. inspect with: docker compose logs --tail=200"
exit 1

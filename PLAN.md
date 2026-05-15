# M365 Audit Ingestor — Technical Plan

> Single-image, dual-role (worker + dashboard) FastAPI application that pulls Microsoft 365 audit events from Graph and the Office 365 Management Activity API and writes them into the existing `z_audit_logs_efk` / `z_audit_logs_efk_runs` tables in MariaDB. Headless by default, with an optional admin UI for config, schema discovery, field mapping, dry-run, and run review.

---

## 0. Decision Record

| # | Decision | Rationale |
|---|----------|-----------|
| D-01 | **Python 3.12 + FastAPI** (single image, two entrypoints) | Mature `msgraph-sdk`, `msal`, `requests`, `SQLAlchemy 2.x`; both M365 APIs first-class; same image runs worker + UI cheaply. |
| D-02 | **Primary source: Microsoft Graph** (`/auditLogs/directoryAudits`, `/auditLogs/signIns`, `/auditLogs/provisioning`). **Secondary: Office 365 Management Activity API** for SharePoint / Exchange / Teams workload events. | Graph covers identity/permission/CRUD on directory objects with delta-friendly cursoring; Mgmt API fills workload gaps. |
| D-03 | **Source label** for both ingestors = `Microsoft365`. Sub-type lives in `extra_data.subsource` (`graph.directoryAudits`, `mgmt.SharePoint`, …). | Matches your column contract; sub-type stays queryable via JSON path. |
| D-04 | **Deployment**: Docker Compose, single host. `deploy.sh` bootstraps secrets, env, TZ, optional MariaDB. | Matches brief. No k8s tax. |
| D-05 | **Discovery UX**: schema sampler + interactive field mapper in dashboard. Persisted as rows in a `mapping_rules` table; exported/imported as YAML for audit. | Lets you bind Graph/Mgmt fields → table columns and `extra_data` JSON keys without code changes. |
| D-06 | **Auth to M365**: confidential client (client credentials flow) for the worker (app-only Graph + Mgmt permissions). Delegated OAuth (auth code + PKCE) only used during the *one-time admin consent* step from the dashboard. | Headless service must not depend on a user being signed in. |
| D-07 | **Idempotency**: `dedup_hash = SHA-256(source || '|' || subsource || '|' || external_event_id || '|' || ISO8601(timestamp_utc))`. Unique index already enforces this. | Replays/backfills become no-ops. |
| D-08 | **State store** for ingestion cursors lives in a small `ingest_state` table (key/value). No second DB. | One persistence boundary. |
| D-09 | **Canonicalize `operation` and `instance`** via a `canonicalize:<table>` mapping transform. Tables live as YAML in `app/mappings/canonical_*.yml`, editable in the dashboard. Raw provider value is always preserved at `extra_data.raw.*`. Unknown values pass through as `unknown.<original_lowered>` rather than failing. | Unifies Graph and Mgmt API vocabulary (e.g. `"Add user"` vs `"Add user."`). The unknown-passthrough rule means new M365 event types never break ingestion — they just lower the coverage SLO until a rule is added. |
| D-10 | **Forward-only ingestion** from first deploy, with a one-shot bootstrap pass: Graph 30 d, Mgmt 7 d. Rows kept indefinitely (no TTL, no archival in v1). | Matches each API's retention horizon. Out-of-band tools own anything older. |
| D-11 | **Failure escalation = Prometheus `/metrics` only.** No webhooks, SMTP, or built-in log shippers in v1. | One notification path; no embedded chatops/SMTP credentials in the deploy footprint. |
| D-12 | **Mapping rules are immutable, versioned.** Every edit inserts a new row with `version=N+1`; the previous row's `valid_to` is stamped. The normalizer picks the row whose `valid_from ≤ event.timestamp < valid_to` (or NULL) for the matching subsource/target. | Lets us answer *"which rule produced this canonical value?"* months later. Hard to retrofit after rules start changing. |
| D-13 | **Audit-of-the-auditor.** Every state-changing admin action (login, mapping edit, backfill trigger, secret rotation, OAuth consent) writes a row to `z_m365ai_admin_events`. Surfaced on a read-only `/admin-events` page. | An audit tool with no self-audit fails its first compliance review. |
| D-14 | **Sovereign clouds via single env var** `AZURE_CLOUD ∈ {commercial,gcc-high,dod,china}`. Endpoint lookup table picks the four base URLs (login authority, Graph, Mgmt, admin-consent). | One-hour change now vs. multi-week migration later. |
| D-15 | **Container hardening + TLS via reverse proxy.** Non-root `USER`, read-only rootfs, dropped caps, `no-new-privileges`. TLS termination is **out-of-process** (Caddy / Traefik) and documented as the deployment pattern; the app itself stays HTTP-only behind the proxy. | Cheap defense-in-depth. Keeps the app from owning cert lifecycle. |
| D-16 | **PII policy: preserve-verbatim by written decision.** `extra_data.raw` is stored exactly as the provider returns it. Confidentiality is enforced at the DB-access boundary, not in-row. The position is recorded here so any future change is a *decision*, not drift. | The simplest defensible posture given controlled DB access. Documented so the next maintainer can't claim it was implicit. |
| D-17 | **Dashboard auth: OIDC/SAML primary, local Argon2id fallback.** When `OIDC_ISSUER_URL` is set, the local login form is disabled and `/login` redirects to the IdP. The bootstrap `DASHBOARD_USER` from `deploy.sh` remains usable for break-glass access only when explicitly enabled via `LOCAL_LOGIN_ENABLED=true`. | An IdP enforces your existing MFA, lifecycle, and offboarding. Break-glass keeps you unstuck when the IdP itself is down. |

---

## 1. Karpathy Principles, applied to this plan

The four guidelines are not decoration — they are **the acceptance contract for every milestone** below.

1. **Think before coding.** Each milestone names its assumptions and the *one* question it answers. If a milestone's pass criteria can't be checked without ambiguity, the milestone is wrong, not the implementation.
2. **Simplicity first.** No framework where a function will do. No background-job framework (Celery/Arq) until M5 proves we need one — `asyncio` + cron-equivalent (APScheduler) is the starting point. No ORM models for tables we don't write to.
3. **Surgical changes.** The DDL you gave is the contract. We do not "improve" your schema (no extra indexes, no renames, no extra FKs). New tables for *our* state (`ingest_state`, `mapping_rules`, `app_users`) are namespaced `z_m365ai_*` so they're trivially droppable.
4. **Goal-driven execution.** Every milestone below has *executable* pass criteria: a `make verify-mN` target that exits non-zero if the goal isn't met. The plan is done when `make verify-all` is green.

---

## 2. High-level architecture

```
                 ┌───────────────────────────┐
   admin browser │   FastAPI Dashboard (UI)  │  basic-auth (deploy.sh creds)
   ────────────► │  /config /m365 /discover  │
                 │  /mapping /runs /dryrun   │
                 │  /healthz /readyz         │
                 └────────────┬──────────────┘
                              │ shares process group, same image
                              ▼
                 ┌───────────────────────────┐
                 │   Ingest Worker (asyncio) │
                 │  ┌──────────┐ ┌─────────┐ │
                 │  │ Graph    │ │ Mgmt    │ │
                 │  │ pollers  │ │ API     │ │
                 │  │ (delta)  │ │ pollers │ │
                 │  └─────┬────┘ └────┬────┘ │
                 │        ▼           ▼      │
                 │     Normalizer (mapping)  │
                 │        │                  │
                 │     Dedup + Batch insert  │
                 └────────────┬──────────────┘
                              ▼
                ┌───────────────────────────┐
                │           MariaDB         │
                │  z_audit_logs_efk         │  (your contract)
                │  z_audit_logs_efk_runs    │  (your contract)
                │  z_m365ai_ingest_state    │  (cursors)
                │  z_m365ai_mapping_rules   │  (field map)
                │  z_m365ai_app_users       │  (dashboard auth)
                └───────────────────────────┘
```

`docker-compose.yml` runs **two services from one image**: `web` (uvicorn) and `worker` (the same package with a different entrypoint). MariaDB is an optional 3rd service if the user doesn't bring their own.

---

## 3. Data contract

### 3.1 Mapping to your `z_audit_logs_efk` columns

| Column | Source field — Graph `directoryAudits` | Source field — Mgmt API record | Notes |
|---|---|---|---|
| `timestamp` | `activityDateTime` (UTC) | `CreationTime` (UTC) | Stored as `DATETIME(3)` UTC. |
| `source` | `"Microsoft365"` (const) | `"Microsoft365"` (const) | Per D-03. |
| `operation` | `activityDisplayName` (e.g. `Add user`, `Update application`) | `Operation` (e.g. `FileAccessed`, `MailItemsAccessed`) | Free text but normalized lower-snake on read. |
| `instance` | `loggedByService` (e.g. `Core Directory`, `Application Proxy`) | `Workload` (`SharePoint`, `Exchange`, `Teams`, …) | Lets you slice by workload. |
| `user_name` | `initiatedBy.user.userPrincipalName` ∥ `initiatedBy.app.displayName` | `UserId` (UPN or app id) | Falls back to `app.displayName` when initiator is service principal. |
| `user_id` | `initiatedBy.user.id` ∥ `initiatedBy.app.servicePrincipalId` | `UserKey` ∥ `UserId` | GUID where available. |
| `extra_data` | full normalized payload (see §3.2) | full normalized payload (see §3.2) | JSON. |
| `comments` | `result` + `resultReason` joined | `ResultStatus` | Short. Truncated to 1024. |
| `dedup_hash` | computed (D-07) | computed (D-07) | Unique. |
| `ingest_run_id` | FK-style link to `z_audit_logs_efk_runs.id` | same | Required. |

`external_event_id`:
- Graph: `id` (event GUID, stable).
- Mgmt API: `Id` (event GUID, stable).

### 3.2 `extra_data` shape (stable JSON contract)

```json
{
  "subsource": "graph.directoryAudits" ,
  "external_id": "f4b1...-...",
  "category": "UserManagement",
  "correlation_id": "...",
  "target_resources": [
    {"type":"User","id":"...","displayName":"...","modifiedProperties":[...]}
  ],
  "initiated_by": {"kind":"user|app", "id":"...", "upn_or_name":"..."},
  "raw": { /* untouched provider payload */ }
}
```

Keeping `raw` lets us evolve normalization without re-pulling.

### 3.3 Auxiliary tables (created by the app)

```sql
CREATE TABLE IF NOT EXISTS z_m365ai_ingest_state (
  k VARCHAR(128) PRIMARY KEY,
  v JSON NOT NULL,
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                  ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS z_m365ai_mapping_rules (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  subsource       VARCHAR(64)  NOT NULL,         -- graph.directoryAudits | mgmt.SharePoint | ...
  target_column   VARCHAR(64)  NOT NULL,         -- operation | instance | user_name | ... | extra_data.<key>
  source_jsonpath VARCHAR(512) NOT NULL,         -- $.activityDisplayName etc.
  transform       VARCHAR(256) DEFAULT NULL,     -- 'coalesce:$.x; canonicalize:operation'
  version         INT UNSIGNED NOT NULL DEFAULT 1,
  valid_from      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  valid_to        DATETIME(3)  DEFAULT NULL,     -- NULL ⇒ currently active
  edited_by       VARCHAR(128) DEFAULT NULL,     -- principal that inserted this version
  UNIQUE KEY uk_rule_version (subsource, target_column, source_jsonpath, version),
  KEY ix_active (subsource, target_column, valid_to)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS z_m365ai_app_users (
  username      VARCHAR(64) PRIMARY KEY,
  password_hash VARCHAR(255) NOT NULL,           -- argon2id; unused when OIDC is configured
  role          VARCHAR(16)  NOT NULL DEFAULT 'admin',
  is_break_glass TINYINT(1)  NOT NULL DEFAULT 0, -- only usable when LOCAL_LOGIN_ENABLED=true
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS z_m365ai_admin_events (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ts          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  actor       VARCHAR(256) NOT NULL,             -- username (local) or OIDC sub@iss
  actor_kind  VARCHAR(16)  NOT NULL,             -- local | oidc | system
  action      VARCHAR(64)  NOT NULL,             -- login.ok | login.fail | mapping.edit | backfill.start | secret.rotate | oauth.consent | config.update
  target      VARCHAR(256) DEFAULT NULL,         -- e.g. "mapping_rule:42" or ".env:AZURE_CLIENT_SECRET"
  request_id  VARCHAR(64)  DEFAULT NULL,         -- W3C traceparent for correlation with logs
  source_ip   VARCHAR(64)  DEFAULT NULL,
  details     JSON         DEFAULT NULL,         -- before/after diff for edits; consent scopes; etc.
  KEY ix_admin_ts (ts),
  KEY ix_admin_actor (actor),
  KEY ix_admin_action (action, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

Default mapping rules are seeded from `app/mappings/default_<subsource>.yml` on first run. Anything the user changes in the UI updates `z_m365ai_mapping_rules`.

### 3.4 Canonical taxonomy (D-09)

`operation` and `instance` are written **canonicalized** into their respective columns. The full untouched provider value is preserved at `extra_data.raw.*` so consumers can always recover the source string.

#### 3.4.1 `operation` — canonical form `noun.verb[.qualifier]`

Lower-snake noun(s), lower verb, dot-separated. Starting taxonomy (full list seeded in `app/mappings/canonical_operations.yml`):

| Canonical | Examples folded into it (Graph → Mgmt) |
|---|---|
| `user.create` | Graph `"Add user"` · Mgmt `"Add user."` |
| `user.update` | Graph `"Update user"` · Mgmt `"Change user password."`, `"Reset user password."` |
| `user.delete` | Graph `"Delete user"` · Mgmt `"Delete user."` |
| `user.signin.success` | Graph signIns where `status.errorCode == 0` |
| `user.signin.failure` | Graph signIns where `status.errorCode != 0` |
| `group.member.add` | Graph `"Add member to group"` · Mgmt `"Add member to group."` |
| `group.member.remove` | Graph `"Remove member from group"` · Mgmt `"Remove member from group."` |
| `role.assignment.add` | Graph `"Add member to role"` · Mgmt `"Add member to role."` |
| `role.assignment.remove` | Graph `"Remove member from role"` |
| `application.consent.grant` | Graph `"Consent to application"` |
| `application.permission.update` | Graph `"Update application"`, `"Add app role assignment"` |
| `sharepoint.file.access` | Mgmt SharePoint `"FileAccessed"` |
| `sharepoint.file.modify` | Mgmt SharePoint `"FileModified"` |
| `sharepoint.file.delete` | Mgmt SharePoint `"FileDeleted"` |
| `exchange.mailitem.access` | Mgmt Exchange `"MailItemsAccessed"` |
| `exchange.send.as` | Mgmt Exchange `"SendAs"` |
| `unknown.<original_lowered>` | **Fall-through.** Triggered when no rule matches. |

#### 3.4.2 `instance` — canonical form (workload, lowercase, no spaces)

| Canonical | Source value(s) |
|---|---|
| `azuread` | Graph `loggedByService="Core Directory"` · Mgmt `Workload="AzureActiveDirectory"` |
| `sharepoint` | Mgmt `Workload="SharePoint"` |
| `exchange` | Mgmt `Workload="Exchange"` |
| `teams` | Mgmt `Workload="MicrosoftTeams"` |
| `apps` | Graph `loggedByService` ∈ {`"Application Proxy"`, `"Application Management"`} |
| `provisioning` | Graph `loggedByService="Account Provisioning"` |
| `signins` | Graph signIns feed (synthetic) |
| `general` | Mgmt `Audit.General` |

#### 3.4.3 Coverage SLO

The dashboard `/mapping` page shows a **coverage gauge**: the share of events ingested in the last 24 h whose canonicalized `operation` did **not** fall through to `unknown.*`. Target ≥ 95 %. When coverage drops below 90 %, the gauge turns red and the page surfaces the top-10 unmapped raw values with a one-click **"Promote to canonical"** workflow that writes a new YAML entry and reloads the canonicalizer without a restart.

Backing metric: `m365ai_events_canonical_unknown_total{subsource,target}` — see §11.3.

### 3.5 Mapping transforms reference

Transforms execute **after** JSON-Path extraction, in the declared order, against the extracted value. Signature: `(value, event_ctx) → value`.

| Transform | Form | Behavior |
|---|---|---|
| `lower_snake` | `lower_snake` | `"Add user"` → `"add_user"`. Non-alphanumeric runs collapse to `_`; the result is lowercased. |
| `truncate:N` | `truncate:1024` | UTF-8-safe truncation; never cuts inside a codepoint. |
| `coalesce:<jsonpath>` | `coalesce:$.initiatedBy.app.displayName` | If the current value is `None`/`""`/`[]`/`{}`, evaluate the alternate JSON-Path against the same event and substitute. Chainable: `coalesce:$.a; coalesce:$.b`. |
| `canonicalize:<table>` | `canonicalize:operation` | Look up the value in `app/mappings/canonical_<table>.yml`. On miss, return `unknown.<value_lowered>` (does not raise). |
| `iso_to_dt3` | `iso_to_dt3` | Parse ISO-8601 with offset → `datetime.datetime` in UTC, microseconds clamped to 3 digits for `DATETIME(3)`. |
| `to_string` | `to_string` | `json.dumps(value, separators=(',',':'), sort_keys=True)` when value is dict/list; identity for scalars. |
| `null_if_empty` | `null_if_empty` | Empty string/list/dict → `NULL`. |

Transforms compose left-to-right with `;`. Example rule:

```yaml
- subsource: graph.directoryAudits
  target_column: operation
  source_jsonpath: $.activityDisplayName
  transform: "coalesce:$.operation; canonicalize:operation"
```

The normalizer **rejects unknown transform names at config load time**, not at first use. Adding a new transform is one function + one registry entry + one fixture test — no schema change.

### 3.6 Mapping-rule versioning (D-12)

Rules in `z_m365ai_mapping_rules` are **append-only**. Edits are inserts of a new `version`, never `UPDATE`s. The active rule for a `(subsource, target_column, source_jsonpath)` triple is the row with `valid_to IS NULL`. When the dashboard saves a change, the write path is:

```
BEGIN;
  UPDATE z_m365ai_mapping_rules
     SET valid_to = NOW(3)
   WHERE subsource = :s AND target_column = :t AND source_jsonpath = :p
     AND valid_to IS NULL;
  INSERT INTO z_m365ai_mapping_rules
    (subsource, target_column, source_jsonpath, transform, version, edited_by)
  VALUES (:s, :t, :p, :new_transform,
          (SELECT COALESCE(MAX(version),0)+1 FROM z_m365ai_mapping_rules
            WHERE subsource=:s AND target_column=:t AND source_jsonpath=:p),
          :actor);
COMMIT;
```

Two consequences:

- **Reproducibility.** Given a row in `z_audit_logs_efk` you can answer "which rule version produced this canonical value?" by looking up the rule whose `valid_from ≤ row.timestamp < COALESCE(valid_to, '9999-12-31')`. The normalizer evaluates rules in that windowed sense; it does **not** retroactively re-canonicalize stored rows.
- **Rollback.** Reverting an edit is *another* insert (a new version whose transform matches an older version), not a `DELETE`. Nothing is ever truly removed.

A small `WHERE valid_to IS NULL` filter on the active-rules query is the entire runtime cost.

### 3.7 Admin event log (D-13)

`z_m365ai_admin_events` is written by a single helper in `app/audit/admin_log.py`. Every state-changing route uses it as a dependency; reads are not logged. Write contract:

```python
def admin_log(action: str, actor: Principal, *, target: str|None=None,
              details: dict|None=None, request: Request) -> None
```

Captured `action` vocabulary:

| Action | When written |
|---|---|
| `login.ok` / `login.fail` | Web `/login` POST. Failures include `details.reason` (`bad_password` / `unknown_user` / `oidc_state_mismatch`). |
| `oauth.consent` | After successful `/m365/callback`. `details.scopes_granted` lists scopes. |
| `mapping.edit` | Every rule version insert. `details` carries the previous + new transform diff. |
| `mapping.promote_canonical` | "Promote to canonical" from §3.4.3 coverage gauge. |
| `backfill.start` | CLI or UI-triggered backfill. |
| `config.update` | Any change to `.env` from the dashboard. `details` lists changed keys (values masked). |
| `secret.rotate` | When a `*_SECRET` / `*_PASS` / `*_KEY` key is changed. |
| `user.create` / `user.disable` | Local user lifecycle. |

The dashboard's `/admin-events` page is **read-only** and filterable by `actor`, `action`, `ts` range. There is no UI to delete rows — by design.

### 3.8 PII policy (D-16)

**Position.** `extra_data.raw` is stored exactly as Microsoft returns it. We do not redact, hash, or drop fields in-row. Confidentiality is enforced at the database access boundary (network ACLs, DB user privileges, at-rest disk encryption owned by the host or the InnoDB tablespace).

**Rationale.** Audit data loses analytical value when redacted ad-hoc, and field-level encryption inside a JSON column produces a key-management problem larger than the one it solves. We document this position so any future change is an *explicit* decision.

**What this means in practice.**

- Personal data definitely present in some events: UPNs, given/family names, IP addresses, user-agent strings, target object GUIDs and display names, file paths, mail subjects (Exchange `MailItemsAccessed`).
- Personal data **not** logged anywhere except the row: `extra_data.raw.*` never appears in `structlog` lines (§11.1).
- Right-to-erasure handling, if ever required, is by targeted `DELETE` against `z_audit_logs_efk` using `user_id` or a JSON predicate on `extra_data.raw`. We do not provide a built-in erasure workflow in v1 — out of scope.

This section is the canonical answer to "how do you handle PII?" The team owning DB access controls owns the rest of the answer.

---

## 4. Authentication & permissions

### 4.1 Microsoft Graph (app-only)

Application (admin-consent) permissions required:

- `AuditLog.Read.All` — directoryAudits, signIns, provisioning.
- `Directory.Read.All` — resolve `target_resources` to display names where needed.
- `User.Read.All` — UPN/displayName lookups for `user_name` fallback.

Auth flow:
- **Bootstrap (one-time, interactive):** Dashboard offers an "Authorize tenant" button → `https://login.microsoftonline.com/{tenant}/adminconsent?...` → returns to `/m365/callback`. We *do not* keep a delegated token; the callback only confirms the consent grant.
- **Runtime (every poll):** Worker uses client-credentials grant against `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` with `scope=https://graph.microsoft.com/.default`. Tokens cached in memory with 90 s skew; refresh on 401.

### 4.2 Office 365 Management Activity API (app-only)

Application permissions (Office 365 Management APIs):
- `ActivityFeed.Read`
- `ActivityFeed.ReadDlp` (optional; needed for DLP records)
- `ServiceHealth.Read` (optional; service health add-on)

Auth flow:
- Same admin-consent URL emits both. Worker uses client-credentials with `scope=https://manage.office.com/.default`.
- **Subscriptions** (`/api/v1.0/{tenant}/activity/feed/subscriptions/start?contentType=Audit.SharePoint`) are created idempotently on worker boot for `Audit.SharePoint`, `Audit.Exchange`, `Audit.AzureActiveDirectory`, `Audit.General`.
- Polling pulls `contentUri` blobs and stores the last `nextPageUri` per content type in `z_m365ai_ingest_state`.

### 4.3 Secret handling

`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` live in `.env` (mode 600, written by `deploy.sh`). The dashboard's *Config* page reads/edits them via a `/config` API that writes back to `.env` and signals the worker via SIGHUP to re-read.

> Assumption surfaced: we are *not* implementing certificate auth in v1. If you have a CA-issued cert for the app reg you'd prefer over a client secret, that's a small extension to `app/auth/msal_client.py` — call it out before M2.

### 4.4 Sovereign clouds (D-14)

`AZURE_CLOUD` in `.env` selects the endpoint set. Default `commercial`. All four endpoints are derived from this one variable — never hardcoded inside the code.

| `AZURE_CLOUD` | Login authority | Graph host | Mgmt API host | Admin-consent host |
|---|---|---|---|---|
| `commercial` | `https://login.microsoftonline.com` | `https://graph.microsoft.com` | `https://manage.office.com` | `https://login.microsoftonline.com` |
| `gcc-high` | `https://login.microsoftonline.us` | `https://graph.microsoft.us` | `https://manage.office365.us` | `https://login.microsoftonline.us` |
| `dod` | `https://login.microsoftonline.us` | `https://dod-graph.microsoft.us` | `https://manage.protection.apps.mil` | `https://login.microsoftonline.us` |
| `china` | `https://login.partner.microsoftonline.cn` | `https://microsoftgraph.chinacloudapi.cn` | `https://manage.office365.cn` | `https://login.partner.microsoftonline.cn` |

Lookup lives in `app/auth/clouds.py`. `deploy.sh` prompts for `AZURE_CLOUD` and validates against the four allowed values. Any value outside the table is a hard error at startup, not silent fallback — sovereign-tenant traffic must never leak to commercial endpoints.

---

## 5. Ingestion pipeline

### 5.1 Graph poller (per audit feed)

```
loop every POLL_INTERVAL_S:
  cursor = state.get(f"graph:{feed}:next_link") or
           "https://graph.microsoft.com/v1.0/auditLogs/{feed}?$filter=activityDateTime ge {LOOKBACK}"
  while cursor:
    resp = GET(cursor)        # retries with exponential backoff on 429/5xx, honors Retry-After
    page = resp.json()
    rows = [normalize(feed, ev) for ev in page['value']]
    insert_batch(rows, run_id)   # ON DUPLICATE KEY do nothing
    cursor = page.get('@odata.nextLink')
  state.set(f"graph:{feed}:next_link", page.get('@odata.deltaLink') or cursor)
finalize_run(run_id, rows_in, rows_inserted, rows_dup)
```

`feed ∈ {directoryAudits, signIns, provisioning}`.

### 5.2 Mgmt Activity poller

```
on boot: ensure subscriptions started for [Audit.SharePoint, Audit.Exchange, Audit.AzureActiveDirectory, Audit.General]
loop every POLL_INTERVAL_S:
  for ct in content_types:
    list_uri = state.get(f"mgmt:{ct}:next_uri") or
               f"/api/v1.0/{tenant}/activity/feed/subscriptions/content?contentType={ct}&startTime=...&endTime=..."
    while list_uri:
      pages = GET(list_uri).json()
      for p in pages:
        blob = GET(p['contentUri']).json()          # list of records
        rows = [normalize_mgmt(ct, ev) for ev in blob]
        insert_batch(rows, run_id)
      list_uri = response.headers.get('NextPageUri')
    state.set(f"mgmt:{ct}:next_uri", None)
```

### 5.3 Run lifecycle (writes to `z_audit_logs_efk_runs`)

- One row per source-day (`source='Microsoft365'`, `report_date=UTC date the cursor advanced through`).
- `started_at` set on first batch of the day; `finished_at` on day rollover or on `verify-runs` reconciliation pass.
- `status ∈ {running, ok, error, partial}`.
- `manual=1` only when triggered from the dashboard.
- The generated `ok_scheduled_date` column gives us the one-row-per-good-day uniqueness for free.

### 5.4 Backfill

`m365ai backfill --since 2026-04-01 --feeds directoryAudits,signIns,Audit.SharePoint`. Same code path; bypasses the cron loop. Writes `manual=1` runs.

### 5.5 First-run bootstrap (D-10)

On first deploy — detected by `z_m365ai_ingest_state` being empty — the worker runs a **one-shot bootstrap pass per feed** before entering the steady-state poll loop:

| Feed | Bootstrap window | Notes |
|---|---|---|
| `graph.directoryAudits` | now − **30 d** → now | Graph retention ≈ 30 d. Page-by-page walk via `$filter=activityDateTime ge ...`. |
| `graph.signIns` | now − **30 d** → now | Highest volume; sized first when estimating event rate. |
| `graph.provisioning` | now − **30 d** → now | Lowest volume. |
| `mgmt.<contentType>` | now − **7 d** → now | Mgmt API retains content blobs ≈ 7 d. The dashboard surfaces this asymmetry on the Runs page. |

Each bootstrap pass materializes as **one** `z_audit_logs_efk_runs` row per feed with `manual=1`, `report_date` = UTC date at start, `status` advancing `running → ok` (or `partial`/`error`). When bootstrap finishes, the next scheduled poll inherits the cursor and resumes with `manual=0`.

Worker-level state machine:

```
[init] ──schema ok──▶ [bootstrap_needed?] ──yes──▶ [bootstrap]
                              │                       │
                              │ no                    │ done
                              ▼                       ▼
                       [steady_state] ◀───────────────┘
                              │
                              ▼
                          (poll loop)
```

`bootstrap_needed?` is true iff *any* configured feed lacks a cursor key in `z_m365ai_ingest_state`. The bootstrap is per-feed idempotent: if the worker crashes mid-pass, the persisted cursor lets the next start resume from exactly where it stopped, and the unique index on `dedup_hash` absorbs any tiny overlap that crosses the crash boundary.

> Surfaced assumption: we do **not** reconcile against any prior ingestor's rows. If you need pre-deploy history beyond the 30 d / 7 d windows, that's an out-of-band load — the dashboard says so on the Runs page when it detects a fresh deploy.

---

## 6. Schema discovery & mapping (the UI)

Two pages:

1. **`/discover`** — pick a feed, pick N (default 50). App makes a live API call, stores the raw payloads in-memory only, infers a *flat* JSON schema (`jsonpath → seen-type, sample, null-rate`). Renders a table.
2. **`/mapping`** — drag/drop or dropdown each inferred jsonpath onto a target column (or `extra_data.<key>`). Save → upsert rows in `z_m365ai_mapping_rules`. "Dry-run" button replays the sampled events through the *current* mapping and shows the row that *would* be inserted, side-by-side with what's already in DB for the same `dedup_hash` (so you can see drift).

The mapper exports/imports YAML so mappings can be code-reviewed.

---

## 7. Database bootstrap

On worker and web startup:

1. Connect (`mysql+pymysql://…` via SQLAlchemy 2.x; `pool_pre_ping=True`).
2. Run `SHOW TABLES LIKE 'z_audit_logs_efk%'`. If missing → execute the two `CREATE TABLE IF NOT EXISTS` statements you provided, **verbatim**. If present → verify column set + key names match expected via `INFORMATION_SCHEMA.COLUMNS` / `KEY_COLUMN_USAGE`. On mismatch, fail loud with a diff and refuse to start — *we do not migrate your tables*.
3. Run the three `z_m365ai_*` `CREATE TABLE IF NOT EXISTS` from §3.3.
4. Seed default mapping rules from `app/mappings/default_*.yml` only if `z_m365ai_mapping_rules` is empty.

---

## 8. Config surface

| Key | Where | Mutable from UI? |
|---|---|---|
| `DB_DSN` | `.env` | Yes (with re-test on save) |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | `.env` | Yes |
| `POLL_INTERVAL_S` (default 300) | `.env` | Yes |
| `GRAPH_LOOKBACK_HOURS` (default 24, only used for first run) | `.env` | Yes |
| `MGMT_CONTENT_TYPES` (csv) | `.env` | Yes |
| `DASHBOARD_USER`, `DASHBOARD_PASS_HASH` | `z_m365ai_app_users` | Yes |
| `TZ` | container env, set by `deploy.sh` | No (redeploy to change) |

UI writes to `.env` go through a single helper that rewrites the file atomically (`tempfile → fsync → rename`) then `kill -HUP $(cat /run/worker.pid)`.

---

## 9. Dashboard

Each page is a single Jinja template + a couple HTMX partials. No SPA.

### 9.1 Pages

- `/login` — dispatches to OIDC or local form per §9.2.
- `/config` — env editor + "Test DB", "Test Graph", "Test Mgmt API" buttons.
- `/m365` — tenant ID, admin-consent link, per-scope consent status.
- `/discover` — feed + N → table of inferred fields.
- `/mapping` — current mapping + edit, import/export YAML, dry-run, coverage gauge (§3.4.3).
- `/runs` — `z_audit_logs_efk_runs` table (filters: source, date, status), drill-in to sample rows + `error_excerpt`.
- `/admin-events` — read-only view of `z_m365ai_admin_events` (filters: actor, action, ts range). **No delete button — by design.**
- `/health` — `/readyz` JSON + last-N-runs sparkline.

### 9.2 Authentication (D-17)

**OIDC/SAML mode (recommended).** Set the following in `.env` and the dashboard switches from local creds to IdP-issued sessions:

| Env | Purpose |
|---|---|
| `OIDC_ISSUER_URL` | Discovery base, e.g. `https://login.example.com` (must serve `/.well-known/openid-configuration`). |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Confidential client registered at the IdP. |
| `OIDC_REDIRECT_URI` | `https://<host>/auth/oidc/callback` (TLS required when OIDC is enabled). |
| `OIDC_REQUIRED_GROUP` | Optional. If set, the `groups` claim must contain this value or login is denied. |
| `OIDC_USERNAME_CLAIM` | Defaults to `preferred_username`. Falls back to `email` then `sub`. |

When `OIDC_ISSUER_URL` is set, the local login form is hidden. `/login` 302s straight to the IdP. The implementation uses `authlib`'s OIDC client with PKCE; no SAML in v1 (most IdPs speak OIDC; SAML is a future extension if needed).

**Local mode (break-glass).** When `OIDC_ISSUER_URL` is unset, the local Argon2id login is the only path. When OIDC is configured but the operator needs to recover from an IdP outage, set `LOCAL_LOGIN_ENABLED=true` in `.env` and `kill -HUP web`. Only `z_m365ai_app_users` rows with `is_break_glass=1` may log in this way. Every break-glass login writes a `login.ok` row to `z_m365ai_admin_events` with `actor_kind='local'` and `details.reason='break_glass'`.

**Session model (both modes).** Stateless JWT signed with `APP_SECRET_KEY` (HS256), 12 h TTL, rotating on use. Cookie is `HttpOnly`, `Secure` (forced when OIDC is on), `SameSite=Lax`. Every state-changing route enforces a double-submit CSRF token bound to the session ID.

### 9.3 Coverage gauge action

The `/mapping` page's coverage gauge (§3.4.3) drives a single button on each unmapped row: **Promote to canonical**. This:

1. Inserts a new `mapping.promote_canonical` admin event.
2. Appends an entry to `app/mappings/canonical_<table>.yml` *in the running container's volume* (so it survives restart) and reloads the canonicalizer in-process.
3. Re-runs the §11 `events_canonical_unknown_total` calculation against the last hour and refreshes the gauge.

No worker restart required.

---

## 10. Containerization

### 10.1 Image

- Base: `python:3.12-slim`, multi-stage build (builder installs deps, runtime carries only `/app` + the wheel-cached venv).
- App runs as **non-root** `USER 10001:10001`; user created in the Dockerfile.
- Read-only root filesystem; writable paths mounted as `tmpfs`: `/tmp`, `/run`.
- `HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1`.
- `ENTRYPOINT ["/usr/local/bin/python","-m"]`, `CMD` differs per service.

### 10.2 Compose stack

`docker-compose.yml` services:

- `web` — `uvicorn app.web:asgi --host 0.0.0.0 --port 8080`. Bound to `127.0.0.1:8080` on the host (not `0.0.0.0`); public exposure is the reverse proxy's job.
- `worker` — `python -m app.worker`.
- `db` — `mariadb:11.4`, opt-in via profile `with-mariadb`, named volume.

Security defaults applied to `web` and `worker`:

```yaml
read_only: true
security_opt: ["no-new-privileges:true"]
cap_drop: ["ALL"]
tmpfs: [ "/tmp", "/run" ]
restart: unless-stopped
```

Volumes: `./data` (mapping YAML exports, run artifacts) and `./logs` are bind-mounted read-write. `.env` is bind-mounted read-only into `web` only; the worker reads its config via SIGHUP-triggered re-read from the shared volume.

### 10.3 TLS — out-of-process reverse proxy (D-15)

The app deliberately does **not** terminate TLS. Documented pattern:

```yaml
# docker-compose.proxy.yml — merge with `docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d`
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["443:443", "80:80"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
volumes:
  caddy_data: {}
  caddy_config: {}
```

```caddy
# Caddyfile
audit.example.com {
    encode zstd gzip
    reverse_proxy web:8080
    header {
        Strict-Transport-Security "max-age=63072000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        Permissions-Policy "interest-cohort=()"
    }
}
```

Caddy handles ACME automatically. Treat the same pattern with Traefik or nginx if those are house standards; the only invariant is that the app listens HTTP on a private port. When OIDC is enabled (§9.2), the app refuses to start unless either `caddy`/`traefik` is present in the compose project **or** `FORCE_INSECURE=true` is set in `.env` (for local development only).

### 10.4 Timezone & locale

`TZ` is set by `deploy.sh` in `.env` and propagated to all three services. Locale is forced to `C.UTF-8` in the image to remove a class of `date`-parsing footguns.

---

## 11. Observability

Single escalation surface per D-11: Prometheus `/metrics`. Logs are stdout JSON for any shipper to consume. No outbound webhooks, no SMTP.

### 11.1 Logging

Sink: stdout. Format: `structlog` JSON. Required keys on every line:

| Key | Value |
|---|---|
| `ts` | RFC 3339 with milliseconds, UTC |
| `level` | `debug` / `info` / `warn` / `error` |
| `event` | machine-readable event name (e.g. `graph.page.fetched`) |
| `service` | `web` or `worker` |
| `request_id` | correlation across web and worker (W3C `traceparent`) |
| `feed` / `subsource` | when applicable |
| `run_id` | when applicable |

PII rule: **never** log `extra_data.raw.*`. Event-level diagnostic lines log only counts and the leading 8 hex chars of `dedup_hash`. UPN appears only at `debug`, which defaults off.

### 11.2 Health endpoints

| Endpoint | Returns 200 iff | Probed by |
|---|---|---|
| `/healthz` | uvicorn process up (always 200 when serving) | Docker `HEALTHCHECK`, every 10 s |
| `/readyz` | `token_age(graph) < 10 min` **AND** `token_age(mgmt) < 10 min` **AND** `db_ping()` **AND** `last_run_age < 4 × POLL_INTERVAL_S` | external monitor |

### 11.3 Metrics catalog (Prometheus text at `/metrics`)

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `m365ai_events_ingested_total` | counter | `subsource` | Events written successfully (post-dedup). |
| `m365ai_events_duplicate_total` | counter | `subsource` | Events that hit the `uk_dedup_hash` unique index. |
| `m365ai_events_canonical_unknown_total` | counter | `subsource`,`target` | Operation/instance values that fell through to `unknown.*`. Drives the §3.4.3 coverage SLO. |
| `m365ai_run_duration_seconds` | histogram | `subsource`,`status` | Wall time of a finalized run. |
| `m365ai_run_status_total` | counter | `subsource`,`status` | One increment per finalized run, by final status. |
| `m365ai_api_request_total` | counter | `api`,`code` | Upstream HTTP status counts (`api` ∈ `graph`/`mgmt`). |
| `m365ai_api_retry_total` | counter | `api`,`reason` | Retries triggered; `reason` ∈ `429`/`5xx`/`network`. |
| `m365ai_api_token_refresh_total` | counter | `api`,`outcome` | Token acquisitions; `outcome` ∈ `ok`/`error`. |
| `m365ai_cursor_lag_seconds` | gauge | `feed` | `now() − activityDateTime` of the most recent ingested event. **Use as the primary "ingest is healthy" signal.** |
| `m365ai_mgmt_subscription_state` | gauge | `content_type` | `1` enabled, `0` disabled (set on each poll). |
| `m365ai_db_pool_in_use` | gauge | — | SQLAlchemy pool checkout count. |
| `m365ai_dashboard_login_failure_total` | counter | — | For external WAF / brute-force alerting. |

### 11.4 `z_audit_logs_efk_runs.status` state machine

```
              ┌───────────┐   first batch insert
   [INIT] ───▶│  running  │──── fatal error ────▶ [error]
              │           │
              └─────┬─────┘
                    │  day rollover OR finalize call
                    ▼
              ┌───────────┐    no batch errors
              │    ok     │
              └───────────┘
              ┌───────────┐
              │  partial  │    ≥1 batch ok AND ≥1 batch non-fatal error
              └───────────┘
```

- **Orphan reconciliation:** on worker startup, any `running` row whose `started_at` is older than `4 × POLL_INTERVAL_S` and which has zero matching rows in `z_audit_logs_efk` since `started_at` is moved to `status='error'` with `error_excerpt='orphaned by restart'`.
- `partial` is reserved for the case where a content-blob fetch failed but the run as a whole made progress. The failed blob URL is recorded in `error_excerpt`; the cursor is **not** advanced past it; the next poll retries it.
- `screenshot_path` is unused for an API-based source (the column was schema-shaped for a browser-scraping ingestor). Always `NULL` here.

### 11.5 Error excerpts

`error_excerpt` is the last stderr/exception frame plus the offending URL where applicable, UTF-8, truncated to 2000 bytes. Stack traces are *not* stored — the matching JSON log line carries the full trace at `level=error` with a correlation `request_id`.

---

## 12. Operational runbook

Each playbook follows the same shape: **Symptom → Detection → Diagnosis → Recovery → Prevention.** All shell commands assume the operator is in the compose project directory on the host.

### 12.1 Stuck Graph cursor (no progress on a feed)

**Symptom.** `m365ai_cursor_lag_seconds{feed="<feed>"}` rises monotonically past 30 min. No new rows in `z_audit_logs_efk` for that feed despite poll attempts in logs.

**Detection.** External rule: `m365ai_cursor_lag_seconds > 1800 for 10m`.

**Diagnosis.**
1. `docker compose logs --since=15m worker | grep '"feed":"<feed>"'` — look for a repeating HTTP status.
2. `SELECT v FROM z_m365ai_ingest_state WHERE k='graph:<feed>:next_link';` — confirm the cursor URL is well-formed.
3. Triage table:
   - 401 looping → token refresh broken → §12.2.
   - 503 / 504 → tenant-side incident; check `https://status.cloud.microsoft`.
   - 400 on `nextLink` → cursor invalidated by tenant reset (rare).

**Recovery (cursor invalidated only).**
```sql
DELETE FROM z_m365ai_ingest_state WHERE k='graph:<feed>:next_link';
```
Then `docker compose restart worker`. Worker re-enters bootstrap mode **for that feed only** and re-walks 30 d. The `uk_dedup_hash` index absorbs the overlap — *no row is double-written.*

**Prevention.** None beyond the health probes. The scenario is rare and self-healing.

### 12.2 Expired or rotated Azure client secret

**Symptom.** `m365ai_api_token_refresh_total{outcome="error"}` climbing; `/readyz` flips to 503.

**Detection.** External rule: `rate(m365ai_api_token_refresh_total{outcome="error"}[5m]) > 0`.

**Diagnosis.** `docker compose logs --since=10m worker | grep AADSTS`. Common codes:
- `AADSTS7000222` — secret expired.
- `AADSTS7000215` — invalid client secret (value mismatch / extra whitespace).
- `AADSTS70011` — scope error (regression after permission change).

**Recovery.**
1. Entra portal → app reg → Certificates & secrets → **New client secret**. Copy the **value** (not the secret ID).
2. On the host:
   ```bash
   sed -i '/^AZURE_CLIENT_SECRET=/d' .env
   ./deploy.sh        # re-prompts only for the deleted key
   docker compose kill -s HUP worker web
   ```
3. Verify `/readyz` returns 200 within ~30 s and `m365ai_api_token_refresh_total{outcome="ok"}` increments on the next poll.

**Prevention.** Calendar reminder 30 d before secret expiry. Cert auth (post-v1) removes the timer.

### 12.3 Schema drift at startup

**Symptom.** Worker (and/or web) exits immediately. Logs: `schema mismatch: z_audit_logs_efk: expected column dedup_hash CHAR(64) NOT NULL, found <something else>`. Exit code `78` (`EX_CONFIG`).

**Detection.** `docker compose ps` shows services `exited`; healthcheck never goes green.

**Diagnosis.** The pre-existing tables don't match the contract in §3 / §7. Causes: another tool created them with different types, or a partial migration was applied out-of-band.

**Recovery — pick one.**
- **Align the existing table to the contract.** The diff in the log enumerates every disagreeing column/index. Apply the corresponding `ALTER TABLE`s.
- **Let the app own the schema.** Only if no other consumer reads these rows:
  ```sql
  DROP TABLE z_audit_logs_efk;
  DROP TABLE z_audit_logs_efk_runs;
  ```
  Restart the stack; the app recreates them from the verbatim DDL.

There is no in-place migration that preserves data when the underlying column **type** is wrong.

**Prevention.** Treat the DDL in §3 as canonical; check it into your DB-schema repo.

### 12.4 Mgmt API subscription disabled

**Symptom.** `m365ai_mgmt_subscription_state{content_type="..."} == 0`. No new rows for that workload.

**Detection.** External rule: `m365ai_mgmt_subscription_state == 0 for 2m`.

**Diagnosis.** The Office 365 Management Activity API auto-disables a content type that hasn't been polled for ~7 d. Usually means the worker was down longer than that. Logs show `subscription is not enabled` on `GET .../content`.

**Recovery.** Restart suffices — `app.ingest.mgmt.ensure_subscriptions()` runs at every worker boot and is idempotent. To force re-enable without a restart:
```bash
docker compose exec worker python -m app.cli mgmt-subs --start Audit.SharePoint
```
The gauge returns to `1` on the next poll. **No data is lost** as long as the gap was ≤ 7 d, since the API still has the content stored — the bootstrap-style window walk picks it up.

**Prevention.** `restart: unless-stopped` covers crashes. For planned maintenance > 7 d, expect to re-enable on return.

### 12.5 Partial-day failure recovery

**Symptom.** `z_audit_logs_efk_runs.status='partial'` for today; some events landed, some didn't.

**Detection.** `m365ai_run_status_total{status="partial"}` increments. UI Runs page shows the row yellow.

**Diagnosis.** Click into `/runs/<id>` — `error_excerpt` shows the offending content blob URL or Graph page that failed.

**Recovery.** None required. The cursor was not advanced past the failure, so the next scheduled poll retries the same blob/page. The unique index absorbs the already-successful events. If failures persist for the same blob across > 3 polls, that's a normalizer bug on a specific event shape — file an issue with the `error_excerpt` and a fixture from `/discover` for that subsource.

**Prevention.** Most partials are transient 5xx. Persistent partials are bugs, not ops.

### 12.6 Replay / explicit backfill

**When to use.** "Reload SharePoint events from 3 days ago."

**Recovery.**
```bash
docker compose exec worker python -m app.cli backfill \
  --subsource mgmt.SharePoint \
  --since 2026-05-11T00:00:00Z \
  --until 2026-05-12T00:00:00Z
```
- Writes a new `z_audit_logs_efk_runs` row with `manual=1`.
- Re-walks the requested window via the standard ingestion path.
- `uk_dedup_hash` makes the operation **idempotent**: re-runs insert zero new rows.

**Caution.** Mgmt API returns `403` if `--since` is older than 7 d. Graph returns empty pages for `> ~30 d` ago. The CLI refuses windows that exceed these and prints why.

**Prevention.** If you frequently feel the urge to backfill "today's data faster", lower `POLL_INTERVAL_S` instead. Backfill is for recovery, not latency tuning.

### 12.7 DB connection lost / pool exhausted

**Symptom.** `m365ai_db_pool_in_use` pinned at the pool ceiling; web returns 500s with `QueuePool limit … overflow … reached`.

**Diagnosis.** `SHOW PROCESSLIST` / `SHOW STATUS LIKE 'Threads_%'` on MariaDB. The usual culprit is a long-running `SELECT` from the dashboard against `z_audit_logs_efk`. The `/runs` page paginates keyset-style on `(timestamp, id)`; if you see offset scans in `EXPLAIN`, you're on an old build — redeploy.

**Recovery.**
1. `KILL <id>` for the runaway query on MariaDB.
2. `docker compose restart web` to reset the pool.

**Prevention.** Default pools: worker `min=2,max=8`, web `min=1,max=5`. Raise only when metrics show sustained saturation, not preemptively.

### 12.8 429 storm from Microsoft Graph

**Symptom.** `rate(m365ai_api_retry_total{api="graph",reason="429"}[5m])` spikes; `m365ai_events_ingested_total` rate drops; `m365ai_cursor_lag_seconds` rises.

**Diagnosis.** Graph applies per-app-per-tenant throttling. The ingestor honors `Retry-After`, so it will eventually recover — but lag will grow during the throttle window.

**Recovery.** No immediate action. If lag breaches your SLO:
- Raise `POLL_INTERVAL_S` (e.g. 300 → 600), `kill -HUP worker`.
- File a Graph throttling-limit increase with Microsoft Support citing the app ID.

**Prevention.** Don't share the same app reg with other heavy Graph consumers.

### 12.9 Disk fill (logs / data volume)

**Symptom.** `df -h` near 100 % on the docker volume disk; containers restart-loop.

**Diagnosis.** Two usual sources: container stdout JSON logs without daemon-level rotation, or the bundled MariaDB volume growing linearly with event volume.

**Recovery.**
- **Container logs.** Configure Docker daemon log rotation:
  ```json
  // /etc/docker/daemon.json
  { "log-driver": "json-file", "log-opts": { "max-size": "50m", "max-file": "5" } }
  ```
  Then `systemctl restart docker`.
- **MariaDB volume.** The app does not delete events by design (D-10). If you must reclaim space, archive out-of-band first, then delete in batches:
  ```sql
  DELETE FROM z_audit_logs_efk
   WHERE source='Microsoft365' AND timestamp < '2025-01-01'
   LIMIT 50000;
  -- repeat until 0 rows affected
  ```

**Prevention.** Capacity-plan against the discover-step row-size estimate: keep enough headroom for ≥ 18 months of expected volume.

### 12.10 Worker crash mid-run

**Symptom.** `z_audit_logs_efk_runs.status='running'` for a row whose `started_at` is older than `4 × POLL_INTERVAL_S`; worker logs show a restart.

**Detection.** Automatic (§11.4 orphan reconciliation).

**Recovery.** None — on next boot the worker reconciles orphaned `running` rows to `error` with `error_excerpt='orphaned by restart'`. The persisted cursor in `z_m365ai_ingest_state` means the next poll resumes from exactly the page that crashed, not from the start of the window.

**Prevention.** The cursor-after-batch design (cursor advances only after a *successful* `INSERT`) makes crash recovery a property of the system, not an operator action. Don't add a "fast" path that commits the cursor before the rows.

### 12.11 Disaster recovery

**Backup target.** MariaDB only. Everything else (config, mapping rules, cursors) is reproducible from `.env` + `app/mappings/*.yml` + a re-bootstrap.

**Procedure.**
1. Nightly: `mysqldump --single-transaction --routines --triggers <db> | gzip > backup-$(date -I).sql.gz`.
2. Restore: `gunzip -c backup-YYYY-MM-DD.sql.gz | mysql <db>`.
3. Start the stack. `z_m365ai_ingest_state` cursors come back with the dump and pick up at the snapshot point.

**RPO / RTO.** The system is append-only by design (D-10). An RPO of 24 h loses ≤ 1 d of events, all of which a `backfill` run (§12.6) will fully recover for both feeds (Graph 30 d > 1 d, Mgmt 7 d > 1 d). RTO is bounded by dump restore time + worker bootstrap (Graph ≈ 30 d × event rate / pages-per-second).

---

## 13. Milestones with executable pass criteria

Each milestone owns a `make verify-mN` target that returns 0 only when the criteria are met. The plan is done when `make verify-all` is green.

### M0 — Repo bootstrap (1 day)
**Deliverables:** package layout (§13), `pyproject.toml`, `Makefile`, `Dockerfile`, `docker-compose.yml`, `.env.example`, pre-commit (ruff + mypy --strict on `app/core`).

**Pass criteria (`verify-m0`):**
- `docker compose build` exits 0.
- `docker compose run --rm web python -c "import app; print(app.__version__)"` prints a SemVer.
- `ruff check .` and `mypy app/core` exit 0.

### M1 — DB bootstrap & contract enforcement (1 day)
**Deliverables:** `app/db/bootstrap.py` that runs §7 and refuses to start on schema mismatch.

**Pass criteria (`verify-m1`):**
- Against an empty MariaDB 11, first startup creates `z_audit_logs_efk`, `z_audit_logs_efk_runs`, and the three `z_m365ai_*` tables; second startup is a no-op (idempotent).
- Pytest case: pre-create `z_audit_logs_efk` *with a wrong column name* (e.g. drop `dedup_hash`) → startup exits non-zero with a diff line containing `expected column dedup_hash`.
- `SELECT COUNT(*) FROM z_m365ai_mapping_rules WHERE subsource='graph.directoryAudits'` ≥ 1 after first boot.

### M2 — Microsoft auth + token caching (1–2 days)
**Deliverables:** `app/auth/msal_client.py` (Graph + Mgmt clients, 401-aware refresh), admin-consent flow at `/m365/authorize` + `/m365/callback`.

**Pass criteria (`verify-m2`):**
- `python -m app.auth.smoke graph` prints a valid token expiry > now.
- `python -m app.auth.smoke mgmt` same.
- Hitting `/m365/authorize` while not logged into the dashboard returns 401.
- Pytest case with a mocked 401 response triggers exactly one refresh and one retry, no infinite loop.

### M3 — Graph ingestion happy path (2 days)
**Deliverables:** Graph poller for `directoryAudits`, normalizer, `insert_batch` with `ON DUPLICATE KEY do nothing`, run-row lifecycle.

**Pass criteria (`verify-m3`):**
- Replaying a canned 1 000-event JSON fixture inserts exactly 1 000 rows on first run, 0 on second run (dedup proven).
- All 1 000 rows have `source='Microsoft365'`, non-NULL `dedup_hash`, valid JSON in `extra_data`, `ingest_run_id` not NULL.
- `z_audit_logs_efk_runs` has exactly one row with `status='ok'`, `rows_in_csv=1000`, `rows_inserted=1000`, `rows_duplicate=0`.
- Canonicalization coverage (§3.4.3) ≥ 95 % across the fixture: `SELECT COUNT(*) FROM z_audit_logs_efk WHERE operation LIKE 'unknown.%';` ≤ 50.
- For every row, `extra_data.raw.activityDisplayName` (or equivalent) equals the original provider value — raw is preserved, never lost.
- Rule versioning (§3.6): editing a mapping rule via the API produces a new row with `version=2` while the previous version's `valid_to` is non-NULL; replaying the *same* fixture against the *new* rule version produces zero duplicates (different `dedup_hash` inputs are still keyed on event, not on rule). The rule used for a given inserted row can be reproduced by joining `valid_from ≤ timestamp < COALESCE(valid_to,'9999-12-31')`.

### M4 — Mgmt Activity ingestion (2 days)
**Deliverables:** subscription start (idempotent), content-list paging, content-blob fetch, normalizer for `Audit.SharePoint` / `Audit.Exchange` / `Audit.AzureActiveDirectory` / `Audit.General`.

**Pass criteria (`verify-m4`):**
- Against a stub of the Mgmt API, ingestor processes a 3-page list with 2 blobs each (= 6 blobs), inserts the union de-duped, advances `mgmt:<ct>:next_uri` to NULL when done.
- Re-running the same window inserts zero new rows.
- `instance` column distribution: at least one row each from SharePoint, Exchange, AzureActiveDirectory.

### M5 — Scheduling & resilience (1 day)
**Deliverables:** APScheduler in `worker.py`, exponential backoff on 429/5xx with `Retry-After`, single-flight per feed (no overlapping pollers), `error_excerpt` capture.

**Pass criteria (`verify-m5`):**
- Synthetic test: stub returns 429 with `Retry-After: 2` on first call, 200 thereafter → poller sleeps ~2 s then succeeds; only one run-row written; `status='ok'`.
- Synthetic test: stub raises 500 ten times → run-row ends as `status='error'`, `error_excerpt` non-empty, length ≤ 2000.
- Killing the worker mid-batch and restarting picks up from the persisted cursor (assertion on `z_m365ai_ingest_state` value).
- Orphan reconciliation pass (§11.4 / §12.10) moves the abandoned `running` row to `error` with `error_excerpt='orphaned by restart'` within one boot cycle.

### M6 — Dashboard: auth + config + discovery + mapping (2–3 days)
**Deliverables:** Pages from §9 except `/runs` and `/health`.

**Pass criteria (`verify-m6`):**
- Bad password returns 401 with no information leak (timing-equalized).
- Editing `POLL_INTERVAL_S` from UI updates `.env`, sends SIGHUP, worker logs `reloaded config` within 5 s.
- `/discover` for `directoryAudits` with N=10 returns a non-empty inferred schema and zero rows are written to the DB (read-only).
- Dry-run on `/mapping` shows a side-by-side diff for at least one sampled event.
- Admin event log (§3.7): every state-changing route — `/login` (ok+fail), `/config` update, `/mapping` edit, `/m365/callback`, backfill trigger — writes the expected `z_m365ai_admin_events` row with non-NULL `actor`, `action`, `request_id`. `/admin-events` page renders the rows in reverse-chronological order and exposes no delete affordance.
- OIDC mode (§9.2): with `OIDC_ISSUER_URL` set against a mock IdP fixture, `/login` 302s to the IdP; a callback with a valid token issues a session cookie; a `groups` claim missing the required value yields a 403 with one `login.fail` event written. With `LOCAL_LOGIN_ENABLED=false`, attempting a local POST to `/login` returns 404. With `LOCAL_LOGIN_ENABLED=true`, only `is_break_glass=1` users can authenticate locally and the resulting `login.ok` event carries `details.reason='break_glass'`.

### M7 — Dashboard: runs + health (1 day)
**Deliverables:** `/runs` table + drill-in, `/health` JSON + sparkline, `/metrics` endpoint.

**Pass criteria (`verify-m7`):**
- `/runs?source=Microsoft365&date=<today>` returns the runs created by M3/M4 verification.
- Clicking into a run row shows 10 sample inserted events and links to the matching `dedup_hash` lookup.
- `curl /metrics | grep m365ai_events_ingested_total` returns a counter > 0.

### M8 — Container & deploy.sh end-to-end (1 day)
**Deliverables:** `deploy.sh`, `.gitignore`, `README.md`, smoke test in compose.

**Pass criteria (`verify-m8`):**
- On a fresh VM: `./deploy.sh` with no args walks the operator through dashboard user, dashboard password, TZ, DB connection (or "spin up local MariaDB"), Azure tenant/client/secret, and writes `.env` mode 600.
- After `deploy.sh` exits, `docker compose ps` shows `web` and `worker` healthy within 30 s.
- `curl -u $USER:$PASS http://localhost:8080/healthz` returns `{"status":"ok"}`.
- Logging in to the dashboard, clicking "Test DB" / "Test Graph" / "Test Mgmt API" all succeed.
- Sovereign cloud (§4.4): setting `AZURE_CLOUD=gcc-high` in `.env` and restarting the worker causes the token-acquisition smoke test to target `login.microsoftonline.us` (asserted via outbound-host log line). Setting `AZURE_CLOUD=bogus` exits the worker non-zero with `unknown AZURE_CLOUD: bogus` — no fallback to commercial.
- Container hardening (§10.1): `docker compose exec web id` returns `uid=10001`; `docker inspect` shows `ReadonlyRootfs:true`, `CapDrop:["ALL"]`, `no-new-privileges:true`.
- TLS pattern (§10.3): merging `docker-compose.proxy.yml` brings up Caddy on `:443` with a valid cert against `https://localhost` via the staging ACME directory (test mode); the app no longer listens on the public interface.
- `verify-all` target runs M0–M7 verifications inside the running container and is green.

---

## 14. Project layout

```
m365ai/
├── app/
│   ├── __init__.py
│   ├── version.py
│   ├── core/
│   │   ├── config.py            # pydantic-settings, .env IO
│   │   ├── logging.py           # structlog
│   │   └── timeutils.py
│   ├── db/
│   │   ├── bootstrap.py         # §7
│   │   ├── engine.py
│   │   └── repo.py              # insert_batch, run lifecycle, state KV
│   ├── auth/
│   │   ├── msal_client.py       # Graph + Mgmt
│   │   ├── consent.py           # admin-consent URL + callback
│   │   ├── clouds.py            # AZURE_CLOUD → endpoint lookup (§4.4)
│   │   ├── oidc.py              # IdP discovery + PKCE flow (§9.2)
│   │   └── local.py             # Argon2id + break-glass policy
│   ├── audit/
│   │   └── admin_log.py         # write-only helper for z_m365ai_admin_events (§3.7)
│   ├── ingest/
│   │   ├── normalizer.py        # mapping engine (jsonpath + transforms)
│   │   ├── dedup.py             # SHA-256
│   │   ├── graph.py             # directoryAudits / signIns / provisioning
│   │   ├── mgmt.py              # Office 365 Management Activity
│   │   └── runner.py            # APScheduler glue
│   ├── mappings/
│   │   ├── default_graph_directoryAudits.yml
│   │   ├── default_graph_signIns.yml
│   │   ├── default_graph_provisioning.yml
│   │   ├── default_mgmt_SharePoint.yml
│   │   ├── default_mgmt_Exchange.yml
│   │   ├── default_mgmt_AzureActiveDirectory.yml
│   │   └── default_mgmt_General.yml
│   ├── web/
│   │   ├── __init__.py          # FastAPI app
│   │   ├── deps.py              # auth dep, db dep
│   │   ├── pages/
│   │   │   ├── login.py
│   │   │   ├── config.py
│   │   │   ├── m365.py
│   │   │   ├── discover.py
│   │   │   ├── mapping.py
│   │   │   ├── runs.py
│   │   │   ├── admin_events.py  # §3.7 read-only page
│   │   │   └── health.py
│   │   └── templates/*.html
│   └── worker.py                # entrypoint for `python -m app.worker`
├── tests/
│   ├── fixtures/                # canned Graph + Mgmt payloads
│   ├── test_bootstrap.py
│   ├── test_dedup.py
│   ├── test_normalizer.py
│   ├── test_graph_poller.py
│   ├── test_mgmt_poller.py
│   └── test_web_auth.py
├── Dockerfile
├── docker-compose.yml
├── deploy.sh
├── Makefile                     # verify-m0 … verify-all
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

---

## 15. Risks, open questions, surfaced assumptions

1. **Tenant licensing.** The Office 365 Management Activity API requires an E3/E5 (or equivalent) license to expose `Audit.*` content. Tenants without unified audit will 403 on subscription start; the worker degrades to Graph-only and logs cleanly. **Confirm tenant SKU before M4.** Ongoing subscription-health failures are §12.4.
2. **`activityDateTime` vs `CreationTime` skew.** Both APIs are UTC but can lag minutes-to-hours. We never re-query an already-cursored window; `dedup_hash` absorbs late-arriving duplicates. This is the deliberate "no fancy watermark" trade.
3. **Mgmt API content retention.** Blobs available ≈ 7 days. Backfill > 7 d from the Mgmt API is **not possible** — the CLI refuses and the UI says so on the Runs page when a fresh deploy is detected.
4. **`ok_scheduled_date` uniqueness.** `UNIQUE (source, ok_scheduled_date)` allows only one successful non-manual run per `(Microsoft365, UTC date)`. We open the run-row on the day's first batch, accumulate counters, finalize at rollover. Manual runs (`manual=1`) bypass per the `CASE` expression.
5. **Canonicalization SLO drift.** Microsoft adds new audit event types continuously. Unknown values fall through to `unknown.*` rather than failing (D-09), but the coverage gauge (§3.4.3) will silently slide if no one reviews it. **Treat coverage < 90 % as a real action item**, not a cosmetic.
6. **Certificate auth.** Not in v1. Client-secret rotation is §12.2; cert auth removes the timer entirely if needed later.
7. **Multi-tenant.** Not in v1. Single-tenant deployment per `.env`.
8. **Forward-only retention (D-10).** Pre-deploy history outside the 30 d / 7 d windows is unreachable from this app — an explicit non-goal. Out-of-band loads are the operator's problem.

---

## 16. Definition of Done (the only thing that matters)

`make verify-all` is green on a fresh VM after `./deploy.sh`, and the following SQL returns rows:

```sql
SELECT source, COUNT(*) AS events, MAX(timestamp) AS latest
FROM z_audit_logs_efk
WHERE source = 'Microsoft365'
GROUP BY source;

SELECT report_date, status, rows_inserted, rows_duplicate
FROM z_audit_logs_efk_runs
WHERE source = 'Microsoft365'
ORDER BY report_date DESC
LIMIT 7;
```

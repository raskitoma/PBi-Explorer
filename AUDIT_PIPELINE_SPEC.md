# M365 / Entra ID Security Audit Pipeline — Technical Specification

**Status:** Draft v0.1 — pending decisions in §11 before implementation begins.
**Owner:** IT + Cybersecurity
**Target deliverable:** A self-hosted web application + background ingestion daemon that authenticates a tenant administrator, configures audit data sources, runs a recurring pull every *N* minutes, and persists normalized records into a relational database that IT and Cybersecurity will query.

---

## 0. How to read this document

This spec is structured under the **Karpathy method** for LLM-assisted coding ([forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)). Four binding principles govern every implementation decision below:

| Principle | Operational rule for this project |
|---|---|
| **Think Before Coding** | Every assumption is named explicitly. Ambiguity is surfaced in §11 (Open Decisions) — *not* silently chosen in code. |
| **Simplicity First** | Minimum code that satisfies the milestone. No speculative abstractions, no "pluggable" anything, no error handling for impossible branches. A senior reviewer must not be able to call the result overcomplicated. |
| **Surgical Changes** | Each milestone touches only the files it must. No drive-by refactors of code from a prior milestone unless that milestone's pass criteria are broken. |
| **Goal-Driven Execution** | Every milestone in §10 ends with **machine-verifiable pass criteria**. A milestone is not "done" until its criteria evaluate true. Bug fixes start with a failing reproduction; features start with a failing assertion. |

If during implementation a milestone's pass criteria appears unreachable, **stop and surface the conflict** rather than weakening the criteria.

---

## 1. Scope

### 1.1 In-scope

Capture, normalize, and persist the following classes of activity for **every user, service principal, and managed identity** in the tenant:

- **Identity lifecycle** — user create / update / delete / restore, attribute changes (UPN, mail, displayName, manager, dept), license assignment changes.
- **Credential events** — password reset (self-service and admin-initiated), password change, MFA method registration / removal, FIDO/WHfB key add/remove, app password issuance.
- **Authorization changes** — directory role assignment/removal, group membership changes (security + M365), administrative-unit scoping, PIM activations.
- **Sign-in activity** — interactive sign-ins, non-interactive sign-ins, service principal sign-ins, managed identity sign-ins, including conditional-access result, risk score, device, IP, location, app, resource.
- **Risk signals** — Identity Protection risk detections, risky users, risky service principals (requires Entra ID P2).
- **Consent / OAuth** — application registrations, service principal creations, app role assignments, delegated permission grants (`oAuth2PermissionGrants`), admin consent events.
- **Service-plane activity (Unified Audit Log)** — Exchange mailbox access (delegate, owner, admin), SharePoint/OneDrive file activity, Teams events, Defender alerts, DLP matches, Power Platform.
- **Security alerts** — Microsoft Graph Security `alerts_v2` (XDR-correlated alerts from Defender for Identity, Defender for Endpoint, Defender for Cloud Apps, Entra Identity Protection).
- **Provisioning** — inbound/outbound provisioning jobs and per-object outcomes (for HR-driven user lifecycle).

### 1.2 Out-of-scope (explicitly)

- Real-time streaming / SIEM-grade event bus. Polling cadence is minutes, not seconds.
- Endpoint telemetry (Defender for Endpoint raw events) — pulled only as it surfaces through `alerts_v2`.
- Network/firewall logs.
- Active Directory on-premises events (would require AD Connect Health or an agent on a DC).
- Mailbox-content inspection (we capture audit metadata, not message bodies).
- Automated response / blocking. This is a read-only auditing system.

---

## 2. Data source catalog

Two parallel data planes must be ingested. They are **not interchangeable** — they cover different events with different schemas, retentions, and auth scopes.

### 2.1 Microsoft Graph (Plane A)

Base: `https://graph.microsoft.com/v1.0` (with selective `beta` use, called out per endpoint).

| Endpoint | Covers | Retention (typical) | Required App permission |
|---|---|---|---|
| `/auditLogs/directoryAudits` | All directory write operations: user CRUD, role assignment, group membership, app/SP creation, consent, policy changes. | 30 days (free/P1/P2) | `AuditLog.Read.All` |
| `/auditLogs/signIns` | Sign-ins. Defaults to interactive; use `$filter=signInEventTypes/any(t: t eq 'nonInteractive')` etc. to broaden. | 7 days (free), 30 days (P1/P2) | `AuditLog.Read.All` |
| `/auditLogs/provisioning` | Entra provisioning job outcomes. | 30 days | `AuditLog.Read.All` |
| `/identityProtection/riskDetections` | Individual risk events. | per P2 retention | `IdentityRiskEvent.Read.All` |
| `/identityProtection/riskyUsers` | Aggregated user risk state. | current state | `IdentityRiskyUser.Read.All` |
| `/identityProtection/riskyServicePrincipals` (beta) | Workload identity risk. | current state | `IdentityRiskyServicePrincipal.Read.All` |
| `/security/alerts_v2` | XDR-aggregated alerts. | 30 days hot | `SecurityAlert.Read.All` |
| `/users`, `/servicePrincipals`, `/groups`, `/directoryRoles` | Enrichment / dimension tables. | live | `Directory.Read.All` (or narrower) |
| `/auditLogs/customSecurityAttributeAudits` | CSA changes — pull only if CSAs are in use. | 30 days | `CustomSecAttributeAuditLogs.Read.All` |

**Cursoring:** Graph supports `$filter` on `activityDateTime` (directoryAudits) and `createdDateTime` (signIns), plus `$orderby`. Persist a high-water-mark (HWM) timestamp per endpoint and query `ge HWM-lookback` each cycle. **Use a configurable lookback overlap of 5 minutes** to absorb late-arriving records; deduplicate on the record `id`.

**Throttling reality** ([Microsoft Graph throttling](https://learn.microsoft.com/en-us/graph/throttling)):
- `/auditLogs/*` has notably stricter quotas than other Graph endpoints.
- Token-bucket algorithm; 429 with `Retry-After` header. Honor it.
- Querying windows longer than ~5–7 days against `signIns` reliably throttles; **keep each request's time window ≤ 24 h** and paginate via `@odata.nextLink`.

### 2.2 Office 365 Management Activity API (Plane B)

Base: `https://manage.office.com/api/v1.0/{tenantId}/activity/feed`

This is the **Unified Audit Log** in API form ([reference](https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-reference)). Content is published as immutable **content blobs** that aggregate events from multiple datacenters. Each content type must be subscribed to once per tenant; thereafter, content blobs become listable.

| Content type | Covers |
|---|---|
| `Audit.AzureActiveDirectory` | Entra audit + sign-ins (overlaps Plane A but with different schema and *longer* retention) |
| `Audit.Exchange` | Mailbox access, mailbox audit, admin actions on Exchange |
| `Audit.SharePoint` | SharePoint + OneDrive file operations, sharing |
| `Audit.General` | Teams, Yammer/Viva Engage, Power Platform, Defender portal actions, eDiscovery, retention, MIP — anything not in the three above |
| `DLP.All` | DLP rule matches |

| Property | Value |
|---|---|
| Required App permission | `ActivityFeed.Read` (+ `ActivityFeed.ReadDlp` for `DLP.All`) |
| Retention | 90 days standard; up to 1 year with E5 + Advanced Audit; up to 10 years with Audit Premium add-on |
| Subscription warm-up | Up to **12 hours** after `/start` before the first blob appears ([reference](https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-reference)) |
| Cursoring | `?contentType=X&startTime=...&endTime=...` returns blob URIs; iterate `NextPageUri` header to paginate |
| Idempotency | Each event has an `Id` GUID; PK on `(content_type, id)` |
| Ordering | **Not guaranteed in-order** within or across blobs — sort downstream by `CreationTime` |

**Implication for the design:** Plane B exists in parallel with Plane A and is **the primary source for non-AAD workloads** (Exchange, SharePoint, Teams, DLP). For AAD itself, prefer Plane A's `directoryAudits` / `signIns` for richness (e.g. ConditionalAccess evaluation details), and use Plane B's `Audit.AzureActiveDirectory` for **retention beyond 30 days**.

### 2.3 Coverage matrix (what answers "all security-related stuff")

| User question | Source |
|---|---|
| "When was this user created? By whom?" | Graph `directoryAudits` (Activity: `Add user`) |
| "Did anyone change this user's UPN, mail, or password?" | Graph `directoryAudits` (Activity: `Update user`, `Reset password (by admin)`) + UAL `Audit.AzureActiveDirectory` |
| "Show all sign-ins for user X in the last 30 days" | Graph `signIns` |
| "Which mailboxes did the admin access yesterday?" | UAL `Audit.Exchange` (`MailItemsAccessed`, `Add-MailboxPermission`) |
| "What files did user Y download from SharePoint?" | UAL `Audit.SharePoint` (`FileDownloaded`, `FileSyncDownloadedFull`) |
| "Did any external user gain access to a Team?" | UAL `Audit.General` (Teams `MemberAdded`, `MemberRoleChanged`) |
| "Were there any risky sign-ins this hour?" | Graph `signIns` (`riskLevelDuringSignIn`, `riskState`) + `riskDetections` |
| "Has any app been granted new delegated permissions?" | Graph `directoryAudits` (`Consent to application`, `Add delegated permission grant`) |
| "Were any DLP policies triggered?" | UAL `DLP.All` |

---

## 3. Identity & permissions model

### 3.1 Two distinct OAuth flows — do not conflate them

| Purpose | Flow | Token type | Notes |
|---|---|---|---|
| **Human admin logs into our web UI** | OAuth 2.0 Authorization Code + PKCE against Entra | User token (delegated) | Used *only* to authenticate operators of our app and gate admin operations (configure connection, view dashboards). Does **not** pull tenant audit data. |
| **Daemon pulls audit data** | OAuth 2.0 Client Credentials grant | App-only token | Background, non-interactive, runs on a schedule. Required because users go offline and refresh tokens expire. |

**This separation is non-negotiable.** If the daemon used delegated tokens, audit ingestion would fail every time the admin's session lapsed.

### 3.2 Entra App Registration — required shape

Register **one** application (single-tenant) with:

**Delegated permissions** (for the web UI sign-in only):
- `openid`, `profile`, `email`, `User.Read`
- `Directory.Read.All` (only if the UI shows a tenant picker / verifier)

**Application permissions** (consented by Global Admin, used by the daemon):
- `AuditLog.Read.All` — Plane A audit + sign-ins + provisioning
- `Directory.Read.All` — dimension enrichment (users, groups, roles)
- `IdentityRiskEvent.Read.All` — P2 only
- `IdentityRiskyUser.Read.All` — P2 only
- `SecurityAlert.Read.All` — Defender XDR alerts
- `ActivityFeed.Read` — Plane B (UAL)
- `ActivityFeed.ReadDlp` — DLP events in Plane B
- `CustomSecAttributeAuditLogs.Read.All` — only if tenant uses CSAs

**Credentials:** Prefer a **certificate** over a client secret for the daemon. Rationale: secrets are bearer credentials with long lifetimes; certs can be rotated via cert store / Key Vault without redeploying code. If the host environment cannot store a private key safely, fall back to a secret with ≤180-day rotation.

**Redirect URIs:** Only the web UI's redirect (e.g. `https://audit.internal.example/auth/callback`). The daemon never uses redirect-based flows.

### 3.3 Least-privilege checklist (apply during PR review)

- [ ] No `Directory.ReadWrite.All`, `User.ReadWrite.All`, or any `*.ReadWrite.*` scope is requested. This system is **read-only**.
- [ ] No `Mail.Read`, `Files.Read`, or content-plane scopes — we read *audit* metadata, not content.
- [ ] Admin consent is documented (who consented, when, what tenant) and re-consent procedure exists.
- [ ] Cert/secret is sourced from a secret store at runtime; **never** committed.

---

## 4. Architecture

The simplest design that satisfies the requirements has **three** components. Resist adding a fourth.

```
┌─────────────────────────────────────────────────────────────────┐
│                         Operator (browser)                       │
│                                │                                 │
│                        Auth Code + PKCE                          │
│                                ▼                                 │
│            ┌──────────────────────────────────┐                  │
│            │  Web app  (1)                    │                  │
│            │  - Admin OAuth login             │                  │
│            │  - Connection config UI          │                  │
│            │  - Dashboards (read-only)        │                  │
│            └──────────────┬───────────────────┘                  │
│                           │ same DB                              │
│            ┌──────────────▼───────────────────┐                  │
│            │  Audit DB  (3)                   │                  │
│            │  - normalized event tables       │                  │
│            │  - dimensions (users/SPs/groups) │                  │
│            │  - cursors / runs / errors       │                  │
│            └──────────────▲───────────────────┘                  │
│                           │ writes                               │
│            ┌──────────────┴───────────────────┐                  │
│            │  Ingestion daemon  (2)           │                  │
│            │  - scheduled poller (every N min)│                  │
│            │  - app-only token acquisition    │                  │
│            │  - Plane A + Plane B pullers     │                  │
│            └──────────────┬───────────────────┘                  │
│                           │  HTTPS                               │
│                  Microsoft Graph + manage.office.com             │
└─────────────────────────────────────────────────────────────────┘
```

### 4.1 Component (1) — Web app

- Single-tenant Entra OAuth (Auth Code + PKCE).
- Authorization gate: only signed-in users whose `oid` is on an allow-list (stored in DB; first user becomes admin on bootstrap) may use the app.
- Three pages: **Connections** (configure tenant + verify daemon), **Pipeline** (last-run status, cursors, error tail), **Explorer** (read-only canned queries over the DB).
- No write-back to Microsoft. Editing audit data is not a feature.

### 4.2 Component (2) — Ingestion daemon

- Long-running process (systemd unit, Windows service, or container).
- Acquires app-only tokens via MSAL (cert preferred, secret allowed).
- One job per (data source, content type) pair; each job owns its cursor row.
- Runs on a cadence configured per source (default 5 min; configurable 1–60 min). **Different sources can run at different intervals** — sign-ins benefit from 1–5 min, UAL blobs from 10–15 min because of publish latency.
- Implements: pagination, throttle backoff (honor `Retry-After`), idempotent upserts, partial-failure isolation.

### 4.3 Component (3) — Audit DB

See §5. Single relational store; no second hot/cold tier in v1.

### 4.4 What we are *not* building in v1

- No message queue (Kafka/Service Bus). The daemon writes directly to the DB.
- No object store for raw payloads. Raw JSON is stored as a `JSONB`/`NVARCHAR(MAX)` column on each event row for forensic replay.
- No separate API service. The web app talks to the DB directly.
- No alerting / SOAR. Cybersecurity uses SQL or BI on the DB.

These omissions are deliberate. They can be added when (and only when) measurable thresholds are crossed (e.g. >5M events/day, multi-region replication required). Adding them now violates Simplicity First.

---

## 5. Database schema (logical)

DBMS is decided in §11. The schema below is dialect-agnostic; types in `<…>` denote logical types resolved per dialect.

### 5.1 Fact tables (events)

#### `event_directory_audit`
| Column | Type | Notes |
|---|---|---|
| `id` | `<uuid>` (PK) | Graph `id`. |
| `activity_datetime` | `<timestamptz>` | Indexed. |
| `activity_display_name` | `<text>` | e.g. `Reset password (by admin)`. |
| `category` | `<text>` | `UserManagement`, `GroupManagement`, etc. |
| `result` | `<text>` | `success` / `failure`. |
| `initiated_by_type` | `<text>` | `user` / `app`. |
| `initiated_by_id` | `<uuid>` | FK → `dim_actor`. |
| `target_resources` | `<jsonb>` | Array of target objects. |
| `additional_details` | `<jsonb>` | `modifiedProperties`, etc. |
| `raw` | `<jsonb>` | Full original record. |
| `ingested_at` | `<timestamptz>` | Default `now()`. |

#### `event_sign_in`
| Column | Type | Notes |
|---|---|---|
| `id` | `<uuid>` (PK) | |
| `created_datetime` | `<timestamptz>` | Indexed. |
| `user_id` | `<uuid>` | FK → `dim_user` (nullable for SP sign-ins). |
| `app_id` | `<uuid>` | App that was signed in to. |
| `resource_id` | `<uuid>` | Resource accessed. |
| `ip_address` | `<inet>` | |
| `client_app_used` | `<text>` | |
| `device_id`, `device_os`, `device_trust_type`, `device_compliant` | | |
| `location_city`, `location_country`, `location_geo_coords` | | |
| `conditional_access_status` | `<text>` | `success`, `failure`, `notApplied`, `unknownFutureValue`. |
| `risk_level_aggregated`, `risk_level_during_signin`, `risk_state`, `risk_detail`, `risk_event_types` | | |
| `auth_requirement` | `<text>` | `singleFactorAuthentication` / `multiFactorAuthentication`. |
| `auth_method_details` | `<jsonb>` | Per-step methods. |
| `status_code`, `status_failure_reason` | | |
| `sign_in_event_types` | `<text[]>` | `interactiveUser`, `nonInteractiveUser`, `servicePrincipal`, `managedIdentity`. |
| `raw` | `<jsonb>` | |
| `ingested_at` | `<timestamptz>` | |

#### `event_provisioning`
Mirrors `directoryAudits` shape with provisioning-specific fields.

#### `event_risk_detection`
Mirrors Graph `riskDetections` resource.

#### `event_security_alert`
Mirrors Graph `security.alerts_v2.alert`.

#### `event_ual`
One table for **all** Plane B events because their schemas vary by `RecordType` ([reference](https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-schema)).

| Column | Type | Notes |
|---|---|---|
| `id` | `<uuid>` (PK) | UAL `Id`. |
| `creation_time` | `<timestamptz>` | Indexed. |
| `content_type` | `<text>` | `Audit.AzureActiveDirectory`, etc. |
| `record_type` | `<int>` | UAL enum. |
| `operation` | `<text>` | e.g. `FileDownloaded`. |
| `organization_id` | `<uuid>` | |
| `workload` | `<text>` | `Exchange`, `SharePoint`, `MicrosoftTeams`, etc. |
| `user_id` | `<text>` | UPN or SID. |
| `user_type` | `<int>` | UAL enum. |
| `client_ip` | `<inet>` | |
| `object_id` | `<text>` | Workload-specific target. |
| `raw` | `<jsonb>` | Full payload — required because RecordType-specific fields are heterogeneous. |
| `ingested_at` | `<timestamptz>` | |

### 5.2 Dimension tables

- `dim_user` — `id`, `upn`, `display_name`, `mail`, `account_enabled`, `created_datetime`, `last_seen_at`. Refreshed by a separate enrichment job every 24 h + on-demand when a fact references an unknown id.
- `dim_service_principal` — `id`, `app_id`, `display_name`, `service_principal_type`, `created_datetime`.
- `dim_group` — `id`, `display_name`, `mail_enabled`, `security_enabled`, `group_types`.
- `dim_directory_role` — `id`, `template_id`, `display_name`.
- `dim_actor` — superset row for any actor referenced in events (user, SP, or app).

### 5.3 Operational tables

- `ingest_cursor` — `(source_key text PK, cursor_value text, last_run_started_at, last_run_finished_at, last_run_status, last_error)`.
  Examples of `source_key`: `graph.directoryAudits`, `graph.signIns`, `graph.signIns.nonInteractive`, `ual.Audit.Exchange`.
- `ingest_run` — append-only history of every run with start/end/duration/records-ingested/error.
- `app_user` — operators allowed to use the web app (`oid`, `upn`, `role`, `added_at`).

### 5.4 Indexing rules

- Every fact table: `(activity_datetime DESC)` and `(user_id, activity_datetime DESC)`.
- `event_sign_in`: additional `(ip_address)`, `(risk_state)`, `(conditional_access_status)`.
- `event_ual`: `(workload, creation_time DESC)`, `(operation, creation_time DESC)`.
- All `raw` columns: do **not** index by default — they are forensic dump, not query plane.

### 5.5 Partitioning

Postgres: declarative range-partition by month on `activity_datetime` / `creation_time` for the fact tables once a single month exceeds ~10M rows. SQL Server: equivalent partition functions. Do **not** partition pre-emptively.

---

## 6. Ingestion mechanics

### 6.1 Graph (Plane A) — algorithm per endpoint

```
loop forever:
  wait until next tick (configured interval)
  hwm := select cursor_value from ingest_cursor where source_key = $endpoint
  start := hwm - 5 minutes overlap
  end   := now() - 1 minute clock-skew margin
  url   := f"{endpoint}?$filter={ts_field} ge {start} and {ts_field} le {end}&$orderby={ts_field} asc&$top=1000"
  while url:
    resp := http_get(url, bearer=app_token)
    if resp.status == 429:
      sleep(resp.headers["Retry-After"]); continue
    if resp.status == 401:
      refresh token; continue (once)
    if resp.status >= 500:
      backoff(exp); continue (until per-run limit)
    upsert(resp.body.value into fact_table on conflict (id) do nothing)
    url := resp.body.@odata.nextLink
  update cursor_value := end where source_key = $endpoint
  record run in ingest_run
```

**Key invariants:**
- `on conflict (id) do nothing` is what makes the 5-min overlap safe.
- The cursor advances only after the **whole** window is persisted; if pagination fails mid-window, the next run re-pulls the entire window.
- Time math is in **UTC** everywhere. The DB stores `timestamptz`.

### 6.2 UAL (Plane B) — algorithm per content type

```
bootstrap (once per content type):
  POST /subscriptions/start?contentType=X
  (wait up to 12 h for first blobs to appear)

per run:
  hwm := cursor for (content_type, "last_blob_creation_time")
  start := hwm - 10 minutes overlap (note: blobs publish late)
  end   := now()
  url   := /subscriptions/content?contentType=X&startTime={start}&endTime={end}
  blob_uris := []
  while url:
    resp := http_get(url, bearer=app_token)
    handle 429/401/5xx as in 6.1
    blob_uris += [b.contentUri for b in resp.body]
    url := resp.headers.get("NextPageUri")
  for blob_uri in blob_uris (parallelism ≤ 4):
    events := http_get(blob_uri).body  # JSON array
    upsert(events into event_ual on conflict (id) do nothing)
  cursor := end
```

**Notes:**
- Subscriptions persist server-side after `/start`; idempotent re-runs of `/start` are safe and return current subscription state.
- Blob contents are emitted out-of-order. **Sort by `CreationTime` only at query time**, not at ingest.
- The 12-hour warm-up is a one-time cost per content type; account for it in milestone planning (§10).

### 6.3 Failure modes and required behavior

| Failure | Required behavior |
|---|---|
| 429 from Graph | Honor `Retry-After`. If absent, exponential backoff starting at 30 s, capped at 5 min. After 6 backoffs in a single run, fail the run and surface in `ingest_run.last_error`; **do not advance cursor**. |
| 401 mid-run | Refresh app token once. If still 401, fail run (likely admin-consent revoked); alert operator via web UI banner. |
| 5xx from Microsoft | Exponential backoff up to 3 retries per request, then fail the run. |
| DB write failure | Fail the run; cursor does not advance; next run re-pulls. Because upserts are idempotent on `id`, replay is safe. |
| Skew where `start ≥ end` | Skip the run (no-op), record `last_run_status = 'skipped'`. |
| Subscription was disabled out-of-band | Detect via `/subscriptions/list`; auto re-`/start`; record in `ingest_run`. |

### 6.4 Why we don't run faster than ~1 min

- `/auditLogs/signIns` throttles aggressively under short, frequent windows. The improvement in freshness below ~1 min is dominated by Microsoft-side ingestion latency (sign-ins can take 5–30 min to appear).
- UAL blobs have publish latency measured in tens of minutes; polling faster than every ~10 min wastes API quota without freshness benefit.

The default interval matrix:

| Source | Default interval | Lookback overlap |
|---|---|---|
| `graph.directoryAudits` | 5 min | 5 min |
| `graph.signIns` | 5 min | 5 min |
| `graph.provisioning` | 15 min | 10 min |
| `graph.riskDetections` | 5 min | 5 min |
| `graph.securityAlerts` | 5 min | 5 min |
| `ual.*` | 15 min | 10 min |
| dimension refresh | 24 h | n/a |

---

## 7. Web app — exact surface

Three routes; nothing more in v1.

### 7.1 `/auth/*`
- `/auth/login` → kicks off Auth Code + PKCE.
- `/auth/callback` → exchanges code for tokens, sets session cookie (HTTP-only, `Secure`, `SameSite=Lax`), redirects to `/`.
- `/auth/logout` → clears session.

### 7.2 `/` (Pipeline page)
Renders, from the DB:
- For each `source_key`: last run start/end/duration, records ingested, current cursor, last error (truncated).
- A "Run now" button per source (POSTs to `/api/run-now` which enqueues a one-shot in the daemon — implemented as a row in a `run_request` table the daemon polls; **no separate queue infra**).
- Aggregated counts: total events ingested in last 24 h by source.

### 7.3 `/connections`
- Shows the configured tenant ID, app ID, credential type (cert thumbprint or secret expiry), admin-consent status.
- "Verify connection" button → triggers a synthetic call to `/auditLogs/directoryAudits?$top=1` and reports success/failure.
- Subscription status for each UAL content type (`enabled` / `disabled` / `not started`).
- Allow-list management (add/remove operator UPNs).

### 7.4 `/explorer`
- Canned read-only queries: "User lifecycle for {upn}", "Sign-ins by {upn} last 30d", "All password resets last 7d", "OAuth consents last 30d", "Risky sign-ins by IP".
- Results render as tables with CSV export.
- **No** ad-hoc SQL in v1. (Direct DB access for analysts is via their own BI tool.)

### 7.5 Out of scope for v1
- Tenant-switching UI (single tenant).
- Role-based access beyond admin / viewer.
- In-app SQL editor.
- Push notifications / email alerts.

---

## 8. Security posture for our own application

- All secrets read from a secret store (Key Vault / `dpapi` / OS keyring / env var sealed at deploy time — pick one per §11). **Never** read from a file checked into the repo.
- DB connection string includes only the audit DB user with `SELECT` on all tables and `INSERT/UPDATE` on event/cursor tables. No `DROP`/`ALTER`. Schema migrations run under a separate migration user.
- Web session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, 8-hour absolute timeout, 30-min idle timeout.
- CSRF: synchronizer-token pattern on every state-changing POST. Reject same-site mismatches.
- TLS: required for `/auth/*` and any production deployment. Localhost is the only HTTP-allowed host.
- Audit *our* app: log every `app_user` action (login, run-now, allow-list change) to a dedicated `app_audit` table. This is meta-audit and is queryable like any other source.
- Dependency hygiene: lockfiles committed; SCA scan in CI.

---

## 9. Observability for the pipeline

Minimum viable telemetry:
- Each run writes a row to `ingest_run`. The web app surfaces the last 50 per source.
- Daemon emits structured logs (one JSON line per event) to stdout. In production these are scraped by whatever the host already runs (e.g. `journalctl`, Windows Event Log, Docker logs).
- A single `/healthz` endpoint on the daemon returns:
  - `200` if all sources have `last_run_finished_at` within `2 × interval` and `last_run_status = 'ok'`.
  - `503` otherwise, with a JSON body listing offenders.

No Prometheus exporter, no OTLP, no APM in v1. If the team already runs an observability stack, the daemon's stdout JSON is sufficient to ingest.

---

## 10. Milestones and pass criteria

Each milestone is a small, mergeable slice. Pass criteria are **objective and runnable** — every "verify" step should be either a test command or a SQL query whose result is deterministic.

### M1 — App registration and credential plumbing
**Deliverables:**
1. Documented Entra app registration with the exact permission set in §3.2.
2. Admin consent recorded; cert (or secret) provisioned and accessible to the daemon's runtime.
3. A `scripts/verify_auth.{py|ts}` that performs the client-credentials grant and prints the resulting access token's `aud`, `roles`, and expiry.

**Pass criteria:**
- `scripts/verify_auth` exits 0, prints `aud=https://graph.microsoft.com`, and `roles` contains every application permission listed in §3.2.
- A second invocation of the script with the daemon's runtime identity (e.g. service account, not developer laptop) also exits 0.
- The same script with an invalidated credential exits non-zero with a clear error.

### M2 — Database schema and migrations
**Deliverables:**
1. Migration files for all tables in §5, runnable forward and backward.
2. Seed data for `dim_directory_role` from the canonical [Entra built-in roles list](https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/permissions-reference).
3. A read-only DB role (`audit_reader`) and an ingest role (`audit_writer`) with least-privilege grants.

**Pass criteria:**
- Migrating up from empty DB then down to empty leaves zero residual objects (`pg_dump --schema-only` / equivalent on SQL Server returns the same diff before and after).
- A test inserts a row into `event_directory_audit` twice with the same `id`; the second insert is a no-op (`on conflict id do nothing`), and a `SELECT count(*)` returns 1.
- `audit_reader` can `SELECT * FROM event_*` but cannot `INSERT`/`UPDATE`/`DELETE`/`ALTER`.

### M3 — Single-source ingestion (`directoryAudits`)
**Deliverables:**
1. A daemon job that pulls Graph `directoryAudits` once on invocation, honoring the algorithm in §6.1.
2. Cursor logic persisted in `ingest_cursor`.
3. Throttle handling — at minimum, honor `Retry-After`.

**Pass criteria:**
- After a synthetic admin action (e.g. creating then deleting a test user via PowerShell), the next ingestion run inserts at least two rows into `event_directory_audit` whose `activity_display_name` matches `Add user` and `Delete user`, and whose `target_resources` includes the test user's id.
- Running the daemon twice in immediate succession produces zero new rows on the second run.
- A simulated 429 (mock at the HTTP client) causes the daemon to sleep ≥ the `Retry-After` value and then succeed; cursor advances only after success.
- A simulated mid-page failure leaves the cursor at its prior value; rerunning ingests the missed records.

### M4 — Scheduler and multi-source ingestion (Plane A)
**Deliverables:**
1. Long-running daemon process with per-source intervals from §6.4.
2. All Plane A sources implemented: `signIns`, `provisioning`, `riskDetections`, `securityAlerts`, plus `directoryAudits` from M3.
3. Dimension enrichment job (24-h cadence) for `dim_user`, `dim_service_principal`, `dim_group`.
4. `/healthz` per §9.

**Pass criteria:**
- Daemon runs uninterrupted for ≥ 24 h with no manual intervention; `ingest_run` shows ≥ 95 % `ok` status across all sources.
- For a user signed in during the run window, `event_sign_in` contains the corresponding row within ≤ `2 × interval + Microsoft-side ingestion latency (≤ 30 min)` of the actual sign-in time.
- `/healthz` returns 200 when all sources are within budget, 503 within 60 s of any source going stale (verifiable by stopping a single source).
- Foreign keys from `event_*.user_id` to `dim_user.id` resolve for ≥ 99 % of rows produced in the last 24 h (the residual is users created during the window — re-checked after the next enrichment pass).

### M5 — Unified Audit Log (Plane B)
**Deliverables:**
1. Subscription bootstrap routine for all five content types.
2. Blob-polling job and `event_ual` writes.
3. Documented handling of the 12-hour warm-up.

**Pass criteria:**
- All five subscriptions report `enabled` via `/subscriptions/list`.
- After warm-up, `event_ual` accumulates ≥ 1 row per content type within 24 h on a non-trivial tenant.
- A re-run of `/subscriptions/start` for an already-enabled content type returns success without duplicating subscriptions.
- Recovery test: disable a subscription out-of-band → daemon's next run detects it, re-enables, and records the action in `ingest_run`.

### M6 — Web app (auth + pipeline status)
**Deliverables:**
1. Auth Code + PKCE login against Entra, session cookie per §8.
2. Allow-list gate; first sign-in seeds the admin row.
3. Pipeline page (`/`) rendering live `ingest_run` data.
4. Connections page (`/connections`) including the "Verify connection" button.

**Pass criteria:**
- A user not on the allow-list lands on a 403 page after successful Entra auth; their attempt is logged in `app_audit`.
- The pipeline page reflects a forced failure (e.g. revoke admin consent → run a job → expect `last_run_status = 'failed'` and a visible error) within one refresh.
- "Verify connection" succeeds when the daemon is healthy and fails (with a meaningful message) when the cert is rotated and not yet redeployed.
- A penetration check confirms session cookie flags (`HttpOnly`, `Secure`, `SameSite`) and CSRF token enforcement on every POST.

### M7 — Explorer and CSV export
**Deliverables:**
1. The five canned queries from §7.4.
2. CSV export for each.
3. Pagination on results (page size 100, capped at 10 k rows per query).

**Pass criteria:**
- Each canned query has a deterministic test fixture: insert a known set of events, run the query, compare result row-by-row.
- CSV exports parse cleanly with a standard CSV reader (no embedded raw newlines breaking row boundaries — escape quotes per RFC 4180).
- Page 2 of a result set with 150 rows returns rows 101–150 in stable order.

### M8 — Hardening pass
**Deliverables:**
1. Dependency lockfile + SCA scan integrated into CI.
2. Secret-leak scan over the repo's history.
3. Backup/restore procedure documented and tested.
4. Run-now requests authenticated and rate-limited (≤ 1 / source / minute).
5. Cert rotation runbook.

**Pass criteria:**
- CI fails on any `high` or `critical` SCA finding without an explicit waiver.
- Secret-leak scan over the entire history returns zero findings.
- Restore from backup yields a DB whose row counts match the source within ±0.1 %; cursors resume forward (no duplicates, no skips).
- Submitting >1 run-now request per source per minute is rejected with 429.

### M9 — Soak + acceptance
**Deliverables:**
1. 14-day continuous-run report.
2. Discrepancy reconciliation: for two random dates in the soak window, compare ingested record counts vs. an independent source (Entra portal UI for `directoryAudits`, `Search-UnifiedAuditLog` PowerShell for UAL).

**Pass criteria:**
- ≥ 99 % of runs in the 14-day window are `ok`. Any `failed` run has a documented post-mortem entry.
- Reconciliation discrepancy is ≤ 1 % for Plane A and ≤ 5 % for Plane B (UAL ordering and publish lag preclude exact match).
- Cybersecurity sign-off on the five canned queries returning expected results for their known test cases.

---

## 11. Open decisions — required input from the game plan

The following decisions are intentionally **not** made in this spec. Each blocks at least one milestone.

| # | Decision | Options & tradeoffs | Blocks |
|---|---|---|---|
| D1 | **Implementation language / runtime** | (a) **Python 3.12 + FastAPI + APScheduler** — fastest MSAL/Graph SDK ergonomics, great for ops scripts. (b) **TypeScript + Node 22 + Express + node-cron** — single language if you want a fancier frontend. (c) **C# + .NET 9** — best M365 SDK fidelity, native on Windows. | All |
| D2 | **Target database** | (a) **PostgreSQL 16** — best JSONB, partitioning, free. (b) **Azure SQL / SQL Server** — fits if BI/IT already standardize there; native partitioning, columnstore for analytics. (c) **SQLite** — only acceptable for dev/PoC, not v1 production. | M2+ |
| D3 | **Hosting** | (a) **Self-hosted Windows Server** (matches `Z:\DProjects` clue). (b) **Azure App Service + Azure SQL** — managed; daemon as a Container App or Web Job. (c) **On-prem Linux** — container-based. | M1, M4 |
| D4 | **Secret store** | Key Vault (Azure-managed) / Windows Cert Store + DPAPI / HashiCorp Vault / env vars sealed at deploy time. | M1, M8 |
| D5 | **Entra ID tier** | If P2 is not licensed tenant-wide, drop `riskDetections` / `riskyUsers` from M4 and adjust pass criteria. | M4 |
| D6 | **Power BI integration** | The dir name `PBi-Explorer` suggests Power BI is in the picture. Decide whether (a) Power BI just reads the DB directly (preferred — no extra work), or (b) we publish a curated semantic model. | M7+ |
| D7 | **Operator allow-list bootstrap** | First Entra sign-in becomes admin, or seeded via a CLI on the host. | M6 |
| D8 | **Polling defaults** | Confirm the §6.4 interval matrix. Any source needing faster/slower? | M4, M5 |

Once these are nailed in your game plan, M1 can begin. Until then, no code is written — per **Think Before Coding**.

---

## 12. Non-goals reminder (anti-scope)

Repeating these because they will tempt scope creep:

- No write-back to Microsoft.
- No alerting/blocking/SOAR.
- No multi-tenant.
- No raw mail/file content.
- No on-prem AD agent.
- No real-time stream.
- No second hot/cold storage tier.

Any of these can be a v2 conversation. They are explicitly *not* part of the success criteria of v1.

---

## Appendix A — References

- [Microsoft Graph audit logs API overview](https://learn.microsoft.com/en-us/graph/api/resources/azure-ad-auditlog-overview?view=graph-rest-1.0)
- [List directoryAudits](https://learn.microsoft.com/en-us/graph/api/directoryaudit-list?view=graph-rest-1.0)
- [List signIns](https://learn.microsoft.com/en-us/graph/api/signin-list?view=graph-rest-1.0)
- [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
- [Microsoft Graph service-specific throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)
- [Office 365 Management Activity API reference](https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-reference)
- [Office 365 Management Activity API schema](https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-schema)
- [AuditLog.Read.All permission details](https://graphpermissions.merill.net/permission/AuditLog.Read.All)
- [Karpathy-method skills (forrestchang/andrej-karpathy-skills)](https://github.com/forrestchang/andrej-karpathy-skills)

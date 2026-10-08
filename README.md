# GitHub Copilot custom reporting

A conditional, aggregate-only reporting implementation for an Enterprise Managed
Users (EMU) enterprise: a standard-library Python collector, protected SQLite
history, and a dependency-free static JavaScript dashboard. **Tenant approval,
representative source-schema validation, and operational acceptance are pending.
This is not a production-ready or approved live deployment.**

[`requirements.txt`](requirements.txt) is the research/specification document,
**not a pip requirements file**. Its source references and acceptance criteria
remain applicable; its original documentation-only baseline predates this code.
The current personal-owner repository is **not** an eligible EMU Pages deployment
location. Use an approved, private, organization-owned **project** repository.

## Try the synthetic dashboard

Prerequisites: Python **3.12+**, Node.js **22+**, and npm. No pip/npm package
installation, enterprise credentials, or network collection is needed.

```sh
npm test
npm run build
npm run preview
```

Open <http://127.0.0.1:8000>. `npm test` runs Python `unittest` and Node's built-in
tests. The build is `python -m copilot_reporting demo --output dist`; preview is
`python -m http.server 8000 --bind 127.0.0.1 --directory dist`. Demo output is
explicitly synthetic. CI tests/builds it on hosted runners and never deploys or
uploads it. Never serve live data with this unauthenticated preview server.

The dashboard provides sortable accessible tables, billing rank bars,
date/model/cost-center filters, and safe filtered-table CSV exports. Daily and
summary CSV datasets are labeled separately: **do not add them together**.
Overview unique-user counts/adoption become N/A under narrowed filters, and
telemetry is hidden for unsupported model/center filters rather than inventing
breakdowns. Prior-period comparison is explicitly unavailable because no
comparison baseline is published.

## Data and access boundary

```text
GitHub APIs / approved local token CSV
  -> trusted collector -> encrypted persistent volume -> SQLite
  -> privacy-approved aggregate JSON + static assets -> private project Pages
```

Raw employee records, token CSVs, export checkpoints, credentials, and the
database stay **outside the repository and publication directory**. The browser
does not call privileged GitHub APIs, accept credentials, or provide an upload
service. Collection snapshots and corrected source periods are replaced
idempotently; unsuccessful sources retain last good partitions with status and
coverage warnings. Protected storage is forbidden inside **any Git working
tree**, and each store is bound to both its enterprise and API origin; do not
reuse a database for a different tenant or host.

**Every site reader can download every published aggregate**, including JSON
hidden by a filter. Pages is not row-level authorization. Different manager or
cost-center audiences, named-user access, and protected web imports require a
separately designed authenticated backend; none is implemented here.

## Protected operator configuration

An operator must provision an encrypted, mounted, persistent volume and a
dedicated Linux runner account before using live collection. Keep the approved
configuration and store on that volume, outside every checkout; for example:

```text
/srv/copilot-reporting/                    # operator-managed encrypted mount
  approved.json                           # runner-owned, mode 0600
  store/                                  # runner-owned, mode 0700
  imports/                                # protected operator imports
```

Copy the **structure** of `config.example.json` into the protected configuration;
do not edit or commit a tenant-specific configuration. The checked-in example
denies publication by default. The parent directories must not be writable by
other users, and protected paths must not be symlinks.

Review and approve:

- The enterprise slug, API version, and API origin: `https://api.github.com` or
  `https://api.<subdomain>.ghe.com`. Collection and deployment origins must agree.
- `enterprise_scope_verified` defaults to `false`. Collection, token import, and
  publication fail closed until an operator proves the credentials' full intended
  enterprise visibility and sets it to `true` in the protected configuration.
  A successful API response alone is insufficient evidence; expiry remains
  available without this gate so privacy cleanup is not blocked.
- `download_hosts`: explicit, exact trusted HTTPS report-download destinations
  verified for the tenant. Empty means no downloads are allowed. Do not broadly
  trust arbitrary storage hosts, copy signed URLs into configuration, or log them.
  Download credentials are not forwarded from the enterprise API.
- Currency and billing scope: `billing_currency` is unset by default. Do not
  assume USD or infer amounts/tokens from prices, credits, or premium requests.
  Obtain finance-approved currency evidence where source amounts omit it.
- `publication.approved`, the `shared-aggregates` audience, and an approved
  `minimum_cohort` (default 5; at least 2). Billing aggregates and cost-center
  breakdowns have separate approval flags. Suppression does not replace a
  privacy review of complementary groups, billing amounts, and repeated releases.
- Raw retention (default **35 days**), aggregate retention (default **395 days**),
  replay window (default **7 days**), and `source_lag_days` (default **3 days**).
  Dates are UTC; absent explicit dates, collection/publication ends at UTC today
  minus `source_lag_days` and covers `replay_days` (seven days by default)—not the
  whole retained history. The three-day default avoids demanding daily partitions
  before normal telemetry publication. The workflow passes no explicit dates,
  so collection and publication use the same configured defaults.

### Credentials and endpoint permissions

Supply credentials through protected environment secrets, never CLI arguments,
JSON configuration, browser variables, logs, issues, PRs, or artifacts:

| Credential | Intended use and approval |
| --- | --- |
| `COPILOT_REPORTING_TOKEN` | Approved enterprise-scoped token. Prefer a dedicated enterprise-installed App with **Enterprise Copilot metrics: read** and **enterprise billing: read**, verifying each endpoint and full enterprise scope. Enable the enterprise metrics policy as documented. |
| `COPILOT_SEATS_TOKEN` | Separately approved roster credential. Enterprise seat endpoint App/fine-grained-token support is **not established** by metrics support. Validate it; if necessary approve a classic-token exception with `read:enterprise` or `manage_billing:copilot` and an authorized enterprise owner/billing manager. |
| Job `GITHUB_TOKEN` | Repository metadata/Pages checks only, **never** reused as the enterprise token or vice versa. Collection has `contents: read`, `pages: read`; deployment separately has `contents: read`, `pages: write`, `id-token: write`. |

The example uses those two enterprise-secret environment names. The workflow
does not mint App tokens: provide approved, valid short-lived material through
your managed credential process and rotate it. A successful restricted API
response does not prove complete enterprise visibility. The sole collection
POST generates a billing export; no license, cost-center, or budget writes occur.

### Commands

Run from a trusted checkout on the protected runner after loading approved
credentials into its environment:

```sh
CONFIG=/srv/copilot-reporting/approved.json
STORE=/srv/copilot-reporting/store

python -m copilot_reporting collect --config "$CONFIG" --store "$STORE"
python -m copilot_reporting import-tokens --config "$CONFIG" --store "$STORE" \
  --file /srv/copilot-reporting/imports/approved-ai-usage.csv \
  --start 2026-10-01 --end 2026-10-07
python -m copilot_reporting publish --config "$CONFIG" --store "$STORE" --output dist
python -m copilot_reporting expire --config "$CONFIG" --store "$STORE"
```

`collect`, `import-tokens`, and `publish` accept `--start`/`--end`; import requires
both to declare the authoritative replacement period. Choose dates within
approved retained history. `collect` returns **2** when sources are unavailable
or failed, after persisting source status and retaining last good partitions;
this is not a complete-success result. Other validation/access failures return
1. Inspect dashboard source freshness, missing dates, and reconciliation rather
than treating an updated build timestamp as fresh data.

Default rolling publication also protects against outages across changing date
windows: if a previously complete source gains missing days in the next
same-length window, publication can retain the prior approved archived window
with refresh-failed/stale status. This fallback requires the **same privacy-policy
fingerprint**. The retained window and original `generated_at` remain visible;
rebuilding the site does not make old data appear freshly generated. Explicit
`--start`/`--end` instead respects the requested range rather than silently
substituting an earlier window.

To restore an **exact previously published and approved window**, including
after its raw records expire:

```sh
python -m copilot_reporting publish --config "$CONFIG" --store "$STORE" \
  --archived --start 2026-10-01 --end 2026-10-07 --output dist
```

The requested archive must still exist under aggregate retention. Restoration
requires the current publication-policy fingerprint to match the archived
policy exactly. A stricter or otherwise different policy does **not** authorize
silently republishing old aggregates: obtain approval and regenerate under the
current policy. Without the necessary retained source records, regeneration may
be unavailable; do not bypass the mismatch check. Restoration does not refresh
the archive's underlying source data.

Token fallback imports are bounded UTF-8 CSV with exact, case-sensitive headers:
`day` (`date` is the only alias), `model`, and at least one of `input`, `output`,
`cache_read`, `cache_write`. Optional identity columns are `user_id`, `username`,
`cost_center_name`, `cost_center_id`. Documented billing metadata columns listed
in `token_import.py` are accepted but not interpreted. Unknown headers, duplicate
grains, malformed counts, or out-of-period rows reject the import. Missing token
categories remain unavailable, not zero. Imported/API-export periods replace,
not add to, each other. Validate representative tenant exports before claiming
token coverage.

## Opt-in private Pages automation

`.github/workflows/reporting.yml` supports manual dispatch and a daily 06:23 UTC
schedule, but is **disabled by default**. It never runs from a PR or a
non-default branch and never enables Pages automatically.

Before enabling it, organization owners must:

1. Provision a private (not internal) organization project repository and
   **already-private**, workflow-based Pages. Organization-root
   `<organization>.github.io` sites and personal-owner repositories are rejected.
2. Protect the default branch and review workflow changes. Configure protected
   environments `reporting-collection` and `github-pages`, restricted to that
   branch with required reviewers. Scope the dedicated self-hosted Linux runner
   labeled `copilot-reporting` to this trusted workflow; never allow untrusted PR
   jobs on a runner with the protected volume.
3. Provision Python 3.12+, required runner action runtimes, encrypted storage,
   least-privilege credentials, backups, and an operator owner. Store enterprise
   secrets **only** in `reporting-collection`; deployment must not have them.
4. Set repository variables after approval:

   | Variable | Required value |
   | --- | --- |
   | `REPORTING_ENABLED` | `true` (leave unset/false until all gates pass) |
   | `REPORTING_APPROVED_ORGANIZATION` | Exact approved repository-owner login |
   | `REPORTING_ENCRYPTION_APPROVED` | `true` only after independently verifying volume encryption and key custody |
   | `REPORTING_STORAGE_MOUNT` | Absolute active encrypted mount, e.g. `/srv/copilot-reporting` |
   | `REPORTING_CONFIG_PATH` | Absolute approved owner-only JSON file on that mount |
   | `REPORTING_STORE_PATH` | Absolute pre-created owner-only persistent store directory on that mount |

The mount check detects a missing mounted volume; the encryption variable is an
**operator attestation**, not software verification of encryption. Backups and
runner workspace disks also need approved protection.

The collector preflight runs **before live collection/publication** and again
before uploading. It uses the repository job token to GET the actual repository
and Pages metadata, requiring organization ownership, `private: true`,
`visibility: private`, Pages `public: false`, and `build_type: workflow`. Origin
validation, bounded responses, no HTTP redirects, and generic errors fail closed.
Privacy approval and protected paths are checked locally. For approved operator
verification, set the same gate variables plus `GITHUB_REPOSITORY`,
`GITHUB_API_URL`, and the **repository-scoped** `GITHUB_TOKEN`, then run:

```sh
python -m copilot_reporting.deployment --config "$CONFIG" --store "$STORE" \
  --storage-mount /srv/copilot-reporting
# After an approved publish, add --output dist to validate its exact file allowlist.
```

Only `dist`'s four static assets and `data/report.json` can enter the Pages
artifact; unexpected files, symlinks, hardlinks, and oversized output are rejected.
No raw snapshots, database, imports, logs, or checkout are uploaded.
Artifact retention is **one day**; artifact access must be approved separately
from Pages access. Self-hosted `dist` is removed in an `always()` cleanup step;
operator recovery must clean it after hard runner failures. A separate hosted
deployment job, protected by `github-pages`, repeats the actual API privacy
preflight immediately before the SHA-pinned deploy action. Privacy changes
between checks and deployment remain an organizational policy/monitoring concern.

## Capability and acceptance status

| Capability | Implemented boundary | Still required before a live pilot |
| --- | --- | --- |
| Usage, licenses, models | Daily source snapshots, stable-user deduplication, observed metrics, unknown/unavailable states | Tenant permissions/schemas, representative reconciliation, sufficient historical roster coverage |
| Billing and cost centers | Source-billed amounts and units, decimals, unallocated/unresolved groups and reconciliation | Currency/scope confirmation; finance approval of allocations and report totals |
| Tokens | Asynchronous export collection and strict local CSV fallback; categories kept separate from credits and CLI/app telemetry | Tenant entitlement, actual export columns, representative token/category reconciliation |
| Center user counts | Intentionally shown as **unavailable**; usage is not mapped to centers from current membership | Historical allocation evidence; billed center amounts do **not** establish licensed/active populations |
| Static privacy | Common-audience approval, cohort suppression, aggregate-only output, guarded private Pages workflow | Tenant gate **not satisfied**; employee/privacy review, artifact/log access approval and denied-access testing |
| Historical operation | SQLite snapshots, ledger, last-good source partitions and approved aggregate archives | Backup restore, retention across all copies, scheduler alerting, scale and recovery acceptance |
| Scoped backend access | **Not implemented** | Separate backend/SSO architecture if audiences differ |

The default seven-day replay/publication window is neither an automatic 13-month
backfill nor publication of all retained history. Approved aggregate archives
remain in SQLite for 395 days and can restore exact approved windows with
`publish --archived`, but raw records expire after 35 days by default.
**Arbitrary rolling unique-user recomputation after raw
expiry is unavailable**; archived aggregates cannot safely be summed to recover
distinct populations. Current seat and center snapshots do not prove historical
license or allocation membership. No provisioned-user/IdP source is implemented.
AI credits, premium requests, interactions, billing tokens, and surface telemetry
tokens are different measures, not interchangeable totals. Subscription totals
and cost-per-active-user are not inferred. Freshness uses source collection
timestamps, including retained data from unavailable sources; an updated site
does not make those sources current.

## Operational acceptance checklist

- Obtain architecture, tenant access/schema, finance, and privacy approval against
  the acceptance criteria in `requirements.txt`; record unavailable requirements.
- Test an authorized repository reader and independently verify that both an
  unauthorized enterprise member and an unauthenticated user cannot fetch the
  site **or its JSON**. These live tests have **not** been performed here.
- Inspect the whole publication and artifact/download permissions, not just the
  UI; test suppression, source outages, permission denials, incomplete exports,
  idempotent corrections, rate limits, and secret/signed-URL log redaction.
- Independently verify encryption, key recovery, protected runner isolation, and
  permissions. Back up SQLite consistently (SQLite backup API or a quiesced
  store), along with approved state/configuration; encrypt and restrict backups,
  and rehearse restoration and deterministic regeneration.
- Run expiry routinely: the CLI applies raw/aggregate retention to SQLite data
  and raw retention to `store/exports/*.json` checkpoints. Separately
  remove expired backups and operator import files. Include old publication copies,
  replaced Pages releases, logs, artifacts, backups, and deleted-user obligations
  in the approved deletion policy. SQLite expiry alone cannot erase external
  copies or guarantee physical erasure on underlying storage.
- Assign credential rotation, schema-change, reconciliation, retention, and
  incident owners. Monitor missed collection cycles (target: alert after two)
  and partial-source warnings; schedule execution is not a freshness guarantee.
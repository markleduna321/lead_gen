# lead_gen

## Accounts and data isolation

Every page and API route requires a signed-in account. Visiting any page while
signed out redirects to `/login`; API calls return `401` with
`{"code": "auth_required"}` so the dashboard can send you back to sign in
instead of failing on an unexpected HTML response.

| Route | Purpose |
| --- | --- |
| `/register` | Create an account (open registration) |
| `/login` | Sign in |
| `/logout` | Sign out (POST) |
| `/api/session` | The signed-in account, or `null` |

Prospects, job leads, and custom hiring services all belong to the account that
discovered them. Two accounts can independently discover the same business or
job posting: uniqueness is per-user (`(user_id, google_place_id)` and
`(user_id, job_identity)`), not global. Reads, writes, exports, scoring,
website checks, follow-up sweeps, and discovery de-duplication are all scoped
to the signed-in account, and discovery runs and their logs are visible only to
their owner. One account's search never blocks another's.

### First-time setup

```powershell
venv\Scripts\python.exe app.py          # start once so migrations create the tables
venv\Scripts\python.exe scripts\reset_multiuser.py --wipe
```

Startup migrations are additive and never modify existing rows, so the
destructive half of the multi-user upgrade lives in `scripts/reset_multiuser.py`
and must be run explicitly, once. Use `--wipe` to start clean or
`--assign-to EMAIL` to give pre-existing leads to a registered account. It also
promotes `user_id` to `NOT NULL`. **Back up first:**
`pg_dump "$env:DATABASE_URL" -f backup.sql`.

### Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | When `APP_ENV` is not `local` | Signs session cookies. Keep it stable — changing it signs everyone out |
| `SMTP_ENCRYPTION_KEY` | To send mail | Encrypts each account's stored SMTP password |
| `APP_ENV` | No (`local`) | `local`/`desktop` keep cookies usable over HTTP; anything else requires HTTPS |
| `FLASK_DEBUG` | No (off) | Debug is opt-in; the Werkzeug debugger is a remote code execution path |
| `MAX_DISCOVERY_WORKERS` | No (`4`) | Discovery searches running at once across all accounts (clamped to 1-32) |
| `APP_HOST` / `APP_PORT` | No | Bind address, default `127.0.0.1:5000` |

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"                      # FLASK_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # SMTP_ENCRYPTION_KEY
```

Passwords are hashed with Werkzeug (scrypt). Sign-in failures return one
generic message whether or not the email exists, and an account locks for 15
minutes after 8 failed attempts.

## Sending email

There is **no shared mailbox**. Each account connects its own SMTP server on
`/settings`, and outreach is sent from — and replies to — that address.
Sending before setting this up returns `409` with
`{"code": "email_not_configured"}`, and the dashboard offers to open Settings.

The signature is also per-account (name, job title, company, phone, website).
Blank fields are omitted, and all values are HTML-escaped.

SMTP passwords are **encrypted, not hashed** — unlike a login password they
have to be replayed to the mail server, so the transformation must be
reversible. The key is `SMTP_ENCRYPTION_KEY` and never lives in the database.

> **Back up `SMTP_ENCRYPTION_KEY` separately from the database.** If it is lost
> or rotated, every stored SMTP password becomes undecryptable and each user
> must re-enter theirs. The app says so explicitly rather than failing silently.

The stored password is write-only: `GET /api/email-settings` returns a
`configured` flag and the non-secret fields, never the password. Saving with a
blank password keeps the stored one, so the form can be edited without the
browser ever receiving the secret. `POST /api/email-settings/test` mails the
account itself and records `verified_at` on success.

Providers using two-factor authentication require an **app password**; spaces
and dashes in pasted app passwords are stripped automatically.

## Workspace

Run `venv\Scripts\python.exe app.py` to open the desktop application.
The shared sidebar links to four independently addressable pages:

| Page | URL | Purpose |
| --- | --- | --- |
| Overview | `/` | Live pipeline counts, recent prospects, recent hiring companies, and next actions |
| Prospects | `/prospects` | Search/filter prospects, inspect profiles, generate pitches, send outreach, and export CSV |
| Job Leads | `/job-leads` | Search/filter hiring companies, inspect job details, and update contact emails |
| Discovery | `/discovery` | Local Businesses and Hiring Companies search forms, map, and worker progress |
| Settings | `/settings` | Your sending mailbox and email signature |

Filters are built from your own leads, so every option matches something in the
list, with a count next to it:

- **Prospects:** Source (the same platform as the row badge), Industry (matching
  ignores letter case and extra spaces, so "Law firm" and "Law Firm" are one
  option), and Status. Search covers name, industry, email, address, and phone.
- **Job Leads:** Job board ("via LinkedIn" and "LinkedIn" are one option),
  Service (built-in and custom), and Status. Search covers company, role,
  location, email, and job board.

**All Active** hides archived leads. Pick **Archived** in Status to see them.
Source/industry/service counts follow the selected status.

Discovery keeps each form's values when switching search modes. After a search
finishes, it opens the corresponding results page. Form values reset on a full
page navigation. Overview metrics exclude archived leads; no demo numbers are
used. Database failures appear explicitly instead of showing empty success data.

### Search outcomes and capacity

A search reports three distinct outcomes:

| Outcome | Meaning | What you see |
| --- | --- | --- |
| Succeeded | Everything worked | Results page plus "N leads saved" |
| Succeeded with warnings | Leads were saved, but some steps failed | Results page plus an amber banner naming what failed |
| Failed | Nothing could be saved | You stay on the form with the error |

A single unreachable contact page no longer reports the whole search as failed
while quietly having saved dozens of leads. Progress also shows the running
saved count, and the summary is carried across the redirect to the results page.

### Provider resilience

SerpApi read timeouts and connection resets are common and usually succeed on a
retry, so every provider call goes through `src/serpapi_client.py`, which
retries transient faults (timeouts, connection errors, HTTP 429/500/502/503/504)
up to three times with exponential backoff. Provider-reported problems such as a
bad key or exhausted quota are **not** retried, since repeating them only wastes
quota. When every attempt fails, the message names the search and the cause
("the search provider timed out") rather than dumping a `HTTPSConnectionPool`
trace.

Per-business enrichment — website lookup, Facebook lookup, email crawl and
screenshot — is best-effort. A failure there is recorded as a warning and the
business is still saved without that detail, instead of discarding every lead
already collected for that industry. If one source fails but another saved
leads, the run succeeds and the failure is shown alongside the results; a run
that saved nothing still fails.

Discovery threads are pooled across every account via `MAX_DISCOVERY_WORKERS`
(default 4), so thread count and SerpApi spend do not grow with the number of
signed-in users. Hitting the pool limit returns `409` with a message about busy
workers; a single account starting a second search gets the more specific
"already running" message instead, and never consumes a slot.

For local-business discovery, type at least three characters for location
suggestions and select a match to update the map pin. **Pin** or starting a
search also resolves edited location text before discovery can run. Failed or
empty lookups block discovery rather than reuse old coordinates. Dragging the
map pin preserves its exact coordinates, even if its address cannot be loaded.
Location lookups use Photon/OpenStreetMap, with debounced suggestions and
in-page caching. Queries and dragged coordinates are sent to Photon. Its public
service permits reasonable usage but can throttle requests and does not
guarantee availability: see [Photon terms](https://photon.komoot.io).

Star and review-count filters apply to **Google Maps and Yelp only**, before
email crawling or screenshots. Minimum stars and maximum reviews are inclusive
(4.5+ includes 4.5; 25 or fewer includes 25). **No Upper Limit** removes the
review-count cap entirely. Results missing a metric required by an active filter
are skipped with a worker log; malformed metrics are also skipped. With both
filters disabled, missing metrics remain unknown rather than fabricated zeros.
Facebook and Instagram leads remain included regardless of these filters,
because these sources do not supply reliable star ratings and review counts.
LinkedIn discovery is not integrated: its checkbox is disabled and the API
rejects LinkedIn selections before starting any worker. Available local-business
discovery sources are Google Maps, Facebook, Instagram, and Yelp.

New discoveries retain Google business website URLs, Instagram emails, and
Facebook/Instagram profile URLs. Social links are stored separately in
`profile_url`, exposed through the prospect queue and CSV export, and labeled
as social profiles in the prospect details; they are not treated as official
websites by scoring or website audits. Startup adds this nullable column
idempotently. This does not repair older discarded contact data or mislabeled
Facebook rows; those require separately approved recovery/backfill work.
Facebook/Instagram discovery uses stable IDs based on canonical profile URLs,
and skips known or repeated profiles before Instagram contact crawling.
Tracking parameters, mobile/www hosts, casing, and trailing slashes do not
create new identities; Facebook numeric profile IDs remain distinct.
Older random-ID records with no saved profile URL cannot be matched reliably.

The map radius is a **strict Google Maps-only** distance filter for pinned
searches: results outside the circle or without valid result GPS coordinates
are logged and skipped before enrichment. The provider's map viewport is only a
search hint, not proof of distance. The API requires a valid pin and a 2-50 km
whole-number radius for Google Maps. Yelp, Facebook, and Instagram use location
text rather than this circle; no extra per-business geocoding is performed.

Industry matching is conservative: Google Maps/Yelp require returned category
metadata matching a preset industry or its aliases, and save the matched
provider category rather than the search term. Facebook/Instagram require
matching title/snippet text; their stored category remains the target industry,
not an independently verified classification. All 32 presets have explicit
aliases; custom industries use normalized phrase matching. Missing evidence is
logged and skipped. This can exclude legitimate businesses with incomplete,
non-English, or unrecognized wording and cannot guarantee real-world relevance.

Local discovery returns a run ID and polls `/api/discovery/runs/<run_id>` for
structured progress and per-run logs. A source/industry failure is recorded while
remaining searches continue; any failure prevents a success redirect and shows
an explicit error. Leads saved before a failure remain available. Only one local
run can execute at a time. **Stop waiting** stops browser monitoring, not the
worker. After repeated status-network failures the UI unlocks with a warning.
Status is in memory (last 20 runs, 50 log lines per run), so an app restart loses
tracking; check Prospects before retrying.

Hiring Companies also uses structured status at
`/api/discovery/job-runs/<run_id>`, with separate logs and a single active hiring
run. Supported service lists and country text are validated, and repeated
service selections scan only once. Search, contact-enrichment, and save failures
are reported as failed/partial runs, not empty successes. Remaining role/service
searches continue when safe; valid postings can be saved before an error is
shown. Malformed postings are individually logged and skipped without discarding
later valid postings. Successful empty searches are distinct from provider
failures. Stop waiting does not cancel a worker; check Job Leads before retrying
after a lost connection or app restart. Bulk-pitch monitoring is unchanged.
Hiring targeting resolves country names/ISO codes using `pycountry`, then sends
both `gl` and `location` to Google Jobs. Unknown countries are rejected before
starting a run. Selected-country searches require explicit country evidence in
the returned posting location: unknown locations, city-only locations, and remote
jobs without that evidence are excluded before contact enrichment. Description
mentions of headquarters do not establish eligibility. Missing locations are
stored as Unknown, not assumed Remote. Blank means no country filter; provider
defaults may bias results and this does not guarantee worldwide coverage.
Actual job titles must match conservative service-specific role patterns,
including common aliases; unrelated titles are logged and skipped. These checks
can exclude legitimate postings with incomplete or unrecognized wording and
do not independently verify employer claims or work authorization.

Hiring deduplication uses a SHA-256 identity of company + actual job title +
returned location + selected service, normalized for Unicode compatibility,
letter case, and whitespace (not punctuation or geographic aliases). The search
role and provider posting ID are not part of this identity. Repeated postings
within a run are merged before enrichment and again at the save boundary:
the first nonempty value wins, with missing details filled by later duplicates,
the longest returned description retained, and metadata lists combined.
Different titles, locations, or selected services remain separate leads.
Identical-looking vacancies with different provider IDs are intentionally merged.
Contact results are cached by normalized company + location, never company alone.
This prevents reuse across different returned locations; public-evidence matching
is described below and does not establish branch ownership.

On the next normal app startup, the migration adds nullable `job_identity`, creates
a unique index for keyed rows, and removes the old company/search-role constraint.
Re-searches update the same keyed lead without resetting its ID, status, draft,
subject, or creation date; empty incoming details do not erase saved details.
**Existing rows are preserved without backfill or historical deduplication.**
Their identity remains NULL; the first re-search may create a new keyed row beside
an old legacy row. Previously overwritten locations cannot be recovered by this
migration. Restart the app before using the updated hiring worker. QA uses mocked
database access and does not apply this migration to the configured database.

Hiring records now retain the full provider-returned description, salary and
schedule text, provider posting ID, reported posting-date text, collection time
in UTC, employment/benefit extensions, highlights, all valid HTTP(S) application
options, and the provider share link. Relative dates such as "3 days ago" remain
provider text rather than invented exact dates. Missing fields remain unavailable.
Duplicate results fill missing details and combine application/highlight lists.
The Job Leads profile shows actual title separately from its searched role signal,
provider-reported metadata, links, and contact provenance. It renders provider
text as text, not HTML.

Company contact lookup requires company-name evidence in both the search-result
title and the candidate website's title or main heading. Known job boards/social
directories are excluded. Up to three candidate hosts are checked; the first
matched site and its `/contact`, `/about`, and `/contact-us` pages are inspected.
Redirects stay on the same host (allowing www/non-www and HTTP-to-HTTPS changes).
Only emails found in visible text/mailto links with the matched website's domain
are retained; off-domain/free-mail addresses and common no-reply/placeholders
are excluded. Publicly listed telephone links, page titles, source URLs, matching
basis, and lookup timestamps are recorded as evidence, not verified identities.
This conservative policy can miss legitimate brands with different legal names,
sites on other domains, JS-only pages, and contacts on other company subdomains.
It cannot disambiguate every same-name business, prove branch ownership or hiring
eligibility, or establish mailbox existence/deliverability. Ambiguous candidates
remain unconfirmed; network/provider failures are logged and mark a partial run.

The next normal startup adds nullable JSONB `job_details` and `contact_evidence`
columns without backfilling existing records. Restart before searching. Existing
contacts without evidence are shown as unconfirmed/legacy, not verified.
Manual email edits are marked unverified and preserved during re-search.
A newly matched website does not inherit an older unconfirmed email when no
current domain-matched email is found. No live migration is applied during QA.
Hiring searches default to **one page per role**. The Pages per Role selector
allows **1, 2, 3, or 5** pages (maximum 5), preserved when switching discovery modes.
Each preset has four role queries; custom services have 1-4. The Google Jobs
budget is `selected role count × pages per role`; the form displays the total.
Five pages for one preset means up to 20 Google Jobs requests, plus separate
company-contact searches. These may consume paid SerpAPI credits. A page contains
up to 10 raw postings before filtering/deduplication; more pages do not guarantee
more matching leads. No automatic retries add to this page budget.

The trigger accepts integer `pages_per_role` (default 1) and returns the accepted
depth and `max_job_requests` along with its run ID. Invalid/unbounded depths are
rejected before starting. Pagination follows only `serpapi_pagination.next_page_token`
at the fixed provider endpoint, keeps country/role parameters, and resets tokens
for each role. It stops at the page budget or when no token remains. Repeated or
malformed tokens and later-page failures produce explicit partial-run errors;
remaining roles/services continue and valid postings can still be saved.
Deduplication covers all fetched pages before contact enrichment. Progress logs
identify the role and page; the run progress counter continues to count services,
not pages. No database-schema change is required for search depth.

### Adding a hiring service

In **Discovery > Hiring Companies**, expand **Add Service**. Enter a service name,
1-4 job-role search phrases (one per line), and 1-12 title-match phrases (one per
line), then select **Save Service**. The service is immediately selected for the
next search and is used for Job Leads table and profile labels; it appears in the
Job Leads Service filter once job leads exist for it.
Saving only registers the definition; it does not launch provider searches.

Names must contain 3-80 characters; search/match phrases must contain 3-100.
Blank, control-character, oversized, and non-text values are rejected. Repeated
phrases are normalized/deduplicated. Returned titles must contain at least one
literal normalized match phrase: matching ignores case, punctuation, and accents,
but does not interpret regex, invent aliases, or inspect the job description.
Enter variants explicitly (for example, "bookkeeper" and "accounts payable").
Country targeting, page limits, deduplication, contact matching, and run/error
reporting are the same as for presets. Broad phrases can still produce
unrelated matches; choose specific role wording.

Custom definitions are stored in the new `hiring_services` table with generated
stable `custom_...` IDs. Name uniqueness ignores case, compatibility Unicode, and
whitespace, including conflicts with preset names. A database unique key also
prevents concurrent duplicate custom names. Existing runs retain immutable
definition snapshots. The five presets and their existing role patterns remain
unchanged; their roles/names now share a registry across the backend and UI.
Request estimates use the actual number of selected role searches, not four
for every custom service.

Restart the app before adding services: the normal startup migration creates
the table if absent without inserting/relabeling historical leads or changing
existing services. The list is reloaded from PostgreSQL on page load, so saved
services survive restarts and are shared by clients using the same database.
GET `/api/hiring-services` lists preset/custom definitions; POST accepts
`name`, `roles`, and `title_phrases` and returns the saved definition (201).
Invalid inputs return 400, duplicate names 409, and database failures 500 with
an explicit UI error/reload option. After a save timeout/lost response, reload
the list before retrying because the database may already have saved it.
Preset searches remain available if the custom catalog cannot be loaded;
the failure is visible, not presented as an empty successful catalog.

This first version is **add-only and discovery-only**: no editing, disabling,
deleting, AI-generated definitions, historical backfill, or custom pitch strategy
is included. Job Leads has no dedicated pitch-generation workflow. Attempts to
use a custom ID through the generic pitch helper raise explicitly rather than
silently generating a web-design pitch. Database migration and SQL/provider I/O
are mocked during QA; no configured database is changed by those checks.

Use **Appearance** in the sidebar to choose System, Light, or Dark. The preference
is saved in the browser. System mode follows the operating system's color theme.
On smaller screens, open the sidebar with the navigation button in the top bar.

Map tiles use `CARTO_BASEMAP_API_KEY` from the local `.env` configuration.
This browser-visible basemap key is separate from server-side discovery and
OpenAI credentials. Do not commit `.env` or other credentials.

## UI regression checks

No additional JavaScript dependencies are required:

```powershell
node --test tests\discovery-ui.test.js tests\workspace.test.js
venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
```

Route tests replace environment loading and database access with mocks. Browser
QA should likewise use an isolated preview: importing the production API module
normally runs startup schema migrations.

Authenticated suites sign in through `tests/support.py`, which writes the
session directly so no users table read is needed. `tests/test_auth_isolation.py`
covers password hashing, non-enumerable sign-in failures, lockout, open-redirect
rejection, the anonymous 401-JSON/302-page split, per-account query scoping, and
run ownership. `tests/test_user_email.py` covers SMTP secret encryption, key
rotation failure, settings validation, signature rendering and escaping,
STARTTLS/SSL connection choice, and the unconfigured-mailbox paths.

Hiring regression coverage includes the trigger-to-worker-to-save-to-results-API
pipeline with real parsing, targeting, pagination, deduplication, contact matching,
and run tracking while provider/SQL I/O is mocked. Success, later-page partial
failure, and database-save failure are checked separately. JavaScript tests also
exercise queue loading, actual-title table selection, full profile metadata,
application links, and the manual-email update flow. These checks do not certify
live credentials, real PostgreSQL migration/upsert execution, contact
deliverability, or visual browser layout.

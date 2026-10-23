# lead_gen

## Workspace

Run `venv\Scripts\python.exe app.py` to open the desktop application.
The shared sidebar links to four independently addressable pages:

| Page | URL | Purpose |
| --- | --- | --- |
| Overview | `/` | Live pipeline counts, recent prospects, recent hiring companies, and next actions |
| Prospects | `/prospects` | Search/filter prospects, inspect profiles, generate pitches, send outreach, and export CSV |
| Job Leads | `/job-leads` | Search/filter hiring companies, inspect job details, and update contact emails |
| Discovery | `/discovery` | Local Businesses and Hiring Companies search forms, map, and worker progress |

Discovery keeps each form's values when switching search modes. After a search
finishes, it opens the corresponding results page. Form values reset on a full
page navigation. Overview metrics exclude archived leads; no demo numbers are
used. Database failures appear explicitly instead of showing empty success data.

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
Each service has four role queries, so its Google Jobs budget is at most
`4 × pages per role` requests; the form displays the total for selected services.
Five pages for one service means up to 20 Google Jobs requests, plus separate
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

Hiring regression coverage includes the trigger-to-worker-to-save-to-results-API
pipeline with real parsing, targeting, pagination, deduplication, contact matching,
and run tracking while provider/SQL I/O is mocked. Success, later-page partial
failure, and database-save failure are checked separately. JavaScript tests also
exercise queue loading, actual-title table selection, full profile metadata,
application links, and the manual-email update flow. These checks do not certify
live credentials, real PostgreSQL migration/upsert execution, contact
deliverability, or visual browser layout.

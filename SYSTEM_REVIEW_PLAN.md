# System Review & Remediation Plan

Multi-phase review of the AsuraTECH Lead Workspace. **No code was changed during
the review.** Each phase records findings first; the implementation plan at the
end is to be executed only after all phases are complete and approved.

| Phase | Scope | Status |
|---|---|---|
| 1 | UX/UI: color, typography, text sizes, copy, link destinations, accessibility | ✅ Complete |
| 2 | UI flows: register, login, plan selection, discovery, validation | ✅ Complete |
| 3 | Backend & security | ✅ Complete |

Severity: **P0** blocks users / legal-commercial risk · **P1** significant UX or
trust problem · **P2** quality and consistency · **P3** polish.

---

## Phase 1: UX/UI review

### Method

- Static review of all templates (`src/templates/*.html`), `static/dashboard.css`,
  `static/home.css`, and UI scripts against the Vercel Web Interface Guidelines
  (fetched at review time) and WCAG 2.2 AA contrast.
- Rendered review in a disposable, offline preview (isolated PostgreSQL, no `.env`
  secrets, outbound requests blocked) at 1440×900 and 390×844, light and dark.
- A new account was registered to review the workspace as a Free Trial user.

### Measurements

**Color contrast (WCAG AA requires 4.5:1 for normal text)**

| Token / pair | Ratio | Result |
|---|---|---|
| Light `--subtle` `#87959e` on `--surface` `#fff` | 3.08 | ❌ Fail |
| Light `--subtle` on `--canvas` `#f5f7f9` | 2.87 | ❌ Fail |
| Light `--muted` `#647782` on `--canvas` | 4.34 | ❌ Fail (borderline) |
| Light `--muted` on `--surface` | 4.66 | ✅ |
| Light `--accent` `#087f68` on `--surface` | 4.95 | ✅ |
| White on primary button `#13795f` | 5.35 | ✅ |
| Dark `--subtle` on `--surface` / `--canvas` | 5.08 / 5.61 | ✅ |
| Dark `--muted`, `--accent` on `--surface` | 7.10 / 9.20 | ✅ |

`--subtle` is used 34 times across CSS, mostly on already-small text.

**Rendered text below 12px (visible text elements)**

| Page | Desktop | Mobile | Smallest |
|---|---|---|---|
| Home `/` (public) | 34 / 80 | 39 / 78 | 7px |
| Plans `/plans` | 36 / 213 | 38 / 211 | 7px |
| Overview | 52 / 87 | 49 / 85 | 8px |
| Prospects | 26 / 68 | 23 / 66 | 8px |
| Job Leads | 24 / 56 | 21 / 54 | 8px |
| Discovery | 38 / 108 | 35 / 106 | 8px |
| Settings | 20 / 55 | 17 / 53 | 8px |
| Billing | 40 / 94 | 37 / 92 | 8px |

The CSS defines 17 distinct font sizes in `dashboard.css` and 21 in `home.css`.
`dashboard.html` uses `text-[10px]` 43 times and `text-xs` 69 times. No page had
horizontal overflow.

### Findings

#### Visual system, color & typography

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| UI-01 | P1 | Text is too small across the product. Primary CTAs and nav links render at 9–11px; kickers, badges, and captions at 7–8px. This hurts readability, especially on mobile. | [home.css](static/home.css) (`.home-kicker` 9px, `.home-nav-cta` 11px, 7px badges); [dashboard.css](static/dashboard.css) (`.brand small` 8px) |
| UI-02 | P1 | Light-theme `--subtle` fails contrast (2.87–3.08:1), and `--muted` fails on the canvas (4.34:1). Both are used for hints, captions, and labels. | [dashboard.css](static/dashboard.css) `:root` |
| UI-03 | P1 | Two styling systems run side by side. Workspace pages load the **Tailwind browser CDN at runtime** and use hard-coded `slate/emerald/amber…` classes (259 `slate` uses), while CSS attribute selectors remap them onto theme tokens. This makes styling fragile, causes a flash of unstyled content, adds a third-party runtime dependency, and needs a CSP exception. | [base.html](src/templates/base.html), [auth.html](src/templates/auth.html), [dashboard.css](static/dashboard.css) (`[class*="bg-slate-950"]`…) |
| UI-04 | P2 | There is no type scale: 17 and 21 distinct pixel sizes, many one-off `font:` shorthands, and heading sizes differ by page (for example, the Billing current-plan `h2` is visually smaller than its body copy). | CSS files; Billing summary |
| UI-05 | P2 | Google Fonts loads through a CSS `@import`, which blocks rendering and chains requests, with 7 + 7 weights. There is no `preconnect` or `preload`. | [dashboard.css](static/dashboard.css) line 1 |
| UI-06 | P2 | Neither theme has a `<meta name="theme-color">`, and native `<select>` styling is inconsistent (the home theme select is 11px on a transparent background). | [public_nav.html](src/templates/public_nav.html), [base.html](src/templates/base.html) |
| UI-07 | P2 | Number and date formats are inconsistent. The top bar shows `30.00` credits while the sidebar shows `30`, and Billing shows `11/3/2026` while the sidebar shows `Nov 3, 2026`. | [account_plan.js](static/account_plan.js) line 75, [billing.js](static/billing.js) |
| UI-08 | P3 | Text uses `...` instead of `…` (51 occurrences in `dashboard.html`), and labels mix "Best Value" and "Best value". | dashboard.html, plans.html, billing.html |
| UI-09 | P3 | Reduced motion is honored for the main animations, but `.home-button:hover` lift and other transitions should be audited together. Animated pulses ("Updating live") have no reduced-motion variant. | home.css, dashboard.css |

#### Copy & content

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| CP-01 | **P0** | **Provider internals and costs are shown to customers.** Hiring discovery says *"4 Google Jobs + 4 LinkedIn Jobs API requests… Job-search requests use 5 Scrapingdog credits each"*, and the local sources mention *"indexed by Google via Scrapingdog"*. This reveals your vendor and cost model and conflicts with the site's own pricing (15 credits per role-page). The hint also reads "Up to 4 pages" while "1 page" is selected. | [dashboard.html](src/templates/dashboard.html) line 1843; discovery sources hint |
| CP-02 | **P0** | **Pricing advertises features that don't exist.** The comparison table lists *Ad Pixel Check, Bulk CSV Enrichment, and Webhook/API Export* as included, while the disclaimer below says they are "planned". This is a consumer-protection risk for paid plans. | [plans.html](src/templates/plans.html) "Other Included Features" row |
| CP-03 | P1 | Internal jargon in the workspace: *"No matching pipeline leads captured inside this view index"*, *"Select any lead row to engage workstation nodes"*, *"Shifting data view scope..."*, *"Transmission Error"*, *"Interactive Target Range Matrix"*, *"Target Test Destination Address"*, *"Inspector Panel"*, *"LEAD ENGINE"*. | [dashboard.html](src/templates/dashboard.html) lines 1224, 1601, 1677, 1705 |
| CP-04 | P1 | Customer pages contain specification-style text. Plans and Billing say *"when payments are configured"*, *"remain visible but locked with an Upgrade to Small Team prompt"*, *"Payments are not configured yet"*. These describe implementation, not customer value. | plans.html "How search credits work"; billing.html |
| CP-05 | P1 | Out-of-date marketing copy. Home says hiring discovery uses Google Jobs only (LinkedIn Jobs is also used) and lists 4 local sources (there are 5, including LinkedIn). Home never mentions the free trial or pricing. | [home.html](src/templates/home.html) |
| CP-06 | P1 | Trial and Solo users see team-only wording: *"shared search credits"*, *"Live shared wallet"*, *"All members share the owner balance and daily cap."* | [account_plan.js](static/account_plan.js) lines 75, 118 |
| CP-07 | P2 | Grammar: *"maximum 1 industries"*. | [account_plan.js](static/account_plan.js) line 118 |
| CP-08 | P2 | Long blocks of help text sit under almost every discovery control (matching evidence, phrase matching, ISO codes, provider pagination). Move it to progressive disclosure ("How this works"). | Discovery page |
| CP-09 | P2 | Emoji used as UI icons (`🔍` in a placeholder, `📋 View More Data`, `🤖 Draft with AI`), and bare `✕` buttons. These are inconsistent with the SVG icon set and read poorly with screen readers. | dashboard.html |
| CP-10 | P2 | "Approve & Send Test" is ambiguous: it doesn't say whether this sends to the prospect or only to a test address. | Prospects inspector |
| CP-11 | P3 | Trial naming is inconsistent: "Site Email/Phone" vs "Website Email/Phone", "Maps / Location Search". | plans.html |

#### Navigation & link destinations

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| NV-01 | **P0** | **Unknown URLs return HTTP 500 with raw JSON** (for example, `/home` or any typo). The global exception handler turns 404 and 405 errors into 500s. There is no 404 page. | [control_gate_api.py](src/control_gate_api.py) `handle_global_exception` |
| NV-02 | **P0** | No legal or support pages anywhere: no Terms, Privacy Policy, Refund Policy, or Contact/Support link. Billing tells users to "contact support" 4 times, with no way to do so. Payment providers and Philippine consumer law expect these. | All footers; billing.html |
| NV-03 | P1 | **Plan CTAs lose the buyer.** "Choose Solo" on `/plans` → `/billing#buy-solo` → `/login?next=/billing` (the `#buy-solo` anchor is lost). A new visitor who clicks "Create an account" loses `next` and lands on Overview instead of checkout. | plans.html; [auth.html](src/templates/auth.html) (register link drops `next`) |
| NV-04 | P1 | Locked pages show raw JSON. A Trial or Solo user opening `/user-management` gets `{"code":"team_owner_required",…}` instead of an upgrade page. | Rendered `/user-management` (403) |
| NV-05 | P1 | The hiring "Upgrade to Small Team" link goes to the public `/plans` page (public nav, no sidebar) instead of in-app `/billing`. | Discovery hiring tab |
| NV-06 | P2 | Discovery CTAs are repeated: the page header "Discover leads" button appears on every page, including Billing and Settings, and Overview shows 3 discovery CTAs at once. Discovery has a "Back to overview" secondary action. | [base.html](src/templates/base.html) `page-heading` |
| NV-07 | P2 | Sidebar structure: "Billing & Plans" sits outside both `<nav>` groups, and "User Management" sits outside the scroll area with no active or `aria-current` state. | base.html |
| NV-08 | P2 | There's no account menu. The profile initials are a non-interactive `<span>`, and there's nowhere to see the signed-in email, change the password, or manage the account. Home and Plans footers don't link to each other consistently. | base.html; home.html footer |
| NV-09 | P3 | The top bar duplicates information already shown in the sidebar: the "AsuraTECH / Lead Gen" tag repeats the brand, and the plan/credit status repeats the sidebar credit card. | base.html |

#### Accessibility & interaction

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| AX-01 | P1 | The prospect and job-profile modals lack `role="dialog"`, `aria-modal`, Escape-to-close, focus trapping, and focus return, and their `✕` close buttons have no `aria-label`. | [dashboard.html](src/templates/dashboard.html) `#job-profile-modal`, `#profile-modal` |
| AX-02 | P1 | Locked states aren't visually distinct. On the hiring tab for a Trial user, the controls look enabled and "Find Hiring Companies" stays bright green even though it is disabled. The upgrade message is small gray text. Locked source checkboxes squeeze "Upgrade to … to unlock" into 3-line fragments. | Rendered Discovery |
| AX-03 | P1 | 8 inputs have no label: `table-search-input`, `filter-sort`, `test-email-input`, `editor`, `modal-radius`, `custom-biz-input`, `modal-rating-min`, `modal-reviews-max`. | Rendered Prospects, Job Leads, Discovery |
| AX-04 | P2 | Pagination buttons `<` and `>` have no accessible names. 27 inline `onclick` handlers in dashboard.html, plus others in base.html, overview.html, and settings.html, block a strict CSP. | dashboard.html |
| AX-05 | P2 | `outline: none` on focused auth and settings inputs relies only on a border-color change. | [dashboard.css](static/dashboard.css) lines 384, 410 |
| AX-06 | P2 | The industry picker is a small nested scroll box with 10+ options, which is hard to scan and touch. There is no select-all or search. | Discovery |
| AX-07 | P3 | `autofocus` on the auth email field opens the keyboard on mobile. | auth.html |

#### Pricing presentation

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| PR-01 | P1 | Prices and limits are hard-coded in `plans.html` and duplicated from `plans.py`/`payments.PRICES`. The plan id `team` vs `small_team` mismatch is patched inline. Future price changes can drift between pages. | [plans.html](src/templates/plans.html) |
| PR-02 | P1 | The discovery form doesn't show the cost of the current search until a `confirm()` dialog after clicking submit. Show a live "This search: 10 credits · balance after: 20" next to the button. | Discovery submit area |
| PR-03 | P2 | Pages-per-role offers 5 pages to every plan; the limit is enforced only by the server. | Hiring tab |
| PR-04 | P2 | The free trial card on `/plans` has no "Start free trial" call to action, only "Compare all features". | plans.html |

### What already works well

- Coherent brand palette and an appealing dark theme. Dark-mode contrast passes everywhere.
- No horizontal overflow on any page at 390px; the plans table scrolls in a labeled, focusable region.
- Skip links, `aria-current` on main nav, `role="status"` on live credit and progress areas, a visible global `:focus-visible` ring, and reduced-motion handling for the globe and progress UI.
- The sidebar credit card, discovery progress overlay, and billing history are clear and informative.

---

## Phase 2: UI flows

### Method

Each flow was exercised end to end in an isolated offline preview (a separate
PostgreSQL database, with all outbound HTTP and SMTP blocked and no `.env`), at
1280/1440 and 390 widths. The flows were driven through the real forms and
buttons, then checked against the server log, database rows, and the source.
Test accounts were disposable (`review@`, `weak@`, `long@example.test`).

### Findings

#### Authentication & account

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| FL-01 | **P0\*** | **Lockout duration depends on the database time zone.** `users.locked_until` is `timestamp without time zone`. The app writes a UTC-aware time; PostgreSQL stores it in the session time zone; `_lockout_remaining` reads it back as UTC. With a UTC+8 database, the 15-minute lock showed **"Try again in 495 minute(s)"** (about 8h15m). With a database west of UTC, the lock would expire immediately and give no protection. \*P0 if the production database time zone is not UTC; check with `SHOW timezone`. | [auth.py](src/auth.py) `_lockout_remaining`, `authenticate`; schema `users.locked_until` |
| FL-02 | P1 | **Account enumeration and targeted lockout.** After 8 wrong passwords, a real account shows the lockout message, while an unknown email never does. Registration also says "An account already exists for that email address". Anyone can lock any known account for 15 minutes (or 8h, see FL-01) just by knowing the email. | Observed: 9 attempts against a real vs an unknown email |
| FL-03 | P1 | No **forgot-password / reset**, no **change password**, and no **email verification**. A user who is locked out or forgets their password has no self-service path, and there's no support link (NV-02). | auth.html; route list |
| FL-04 | P2 | Password strength is length-only: `aaaaaaaaaa` was accepted. There's no check against common or breached passwords and no strength meter. | Registered `weak@example.test` |
| FL-05 | P2 | Registration errors show as a single banner above the form, not next to the field that caused them. There's no `aria-invalid`, the error isn't linked to its field, and focus isn't moved to the problem field. Good: name and email are kept, and passwords are cleared. Display names over 120 characters are silently truncated. | Rendered `/register` errors |
| FL-06 | P1 | **`next` is lost when switching between Sign in and Create account.** The login page's "Create an account" link is plain `/register`. A visitor sent to `/login?next=/billing` who has no account ends up on Overview after registering. (Extends NV-03.) | [auth.html](src/templates/auth.html) lines 61–63 |

#### Team invitations

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| FL-07 | **P0** | **Signed-out invitees lose the invitation.** The emailed link is `/team-invitation#<token>`, with the token in the URL fragment. The page's "Sign in" and "Create account" buttons go to `?next=/team-invitation`, which drops the fragment. After signing in, the user lands on the page without the token, yet "Accept invitation" is still enabled. New team members can't join unless they reopen the email link after signing in. | Reproduced: `/team-invitation#tok123` → Sign in → `/team-invitation` (token gone) |
| FL-08 | P2 | The signed-out invitation page uses the full workspace layout: sidebar nav, a "Log out" button, a placeholder "AT / AsuraTECH Solutions" avatar, and a credit card stuck on "Loading… / Connecting to wallet…". | Rendered `/team-invitation` while signed out |
| FL-09 | P3 | An invalid token shows "Invitation is invalid or expired. **Reload to check the outcome before retrying.**" The generic retry suffix doesn't fit, and the error doesn't explain what to do next (ask the owner to resend). | Rendered `#badtoken` |

#### Discovery

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| FL-10 | P1 | **Plan limits aren't reflected in the form.** A Trial user (1 industry) could tick several industries; only the server rejects it, after the cost confirm. There's no "1 of 1 selected" counter and extra boxes aren't disabled. | Ticked 3 industries as a Trial user; server limit in [plans.py](src/plans.py) |
| FL-11 | P1 | **The confirm dialog appears late and out of context.** Clicking "Find Businesses" first resolves the location (only a small "Resolving location..." note), then a native `confirm()` appears seconds later, even if the user has moved on to other controls. The button doesn't show a busy state while the location resolves. | [dashboard.html](src/templates/dashboard.html) `submitDiscoveryRequest`; observed while clicking a locked source |
| FL-12 | P2 | "Roofing Contractor" is pre-selected on every visit, which invites an unintended paid search. | Default checked: `google`, `Roofing Contractor` |
| FL-13 | P2 | When everything fails, the message says "Discovery finished with errors. **Some leads may have been saved.**", even though 0 were saved and nothing was charged (balance stayed 30). It should say "No leads found · no credits charged". The live log shows internal phrasing ("Handshake network drop", "[PROVIDER RETRY]"). | Submitted with providers blocked |
| FL-14 | P1 | **Script injection in the custom business type box.** Typing `<img src=x onerror=…>` and clicking "+ Add" ran the script. Right now it only affects the person typing it (not saved), but it uses the same unsafe pattern Phase 3 will check for scraped data. | `addCustomBizType` (`label.innerHTML` with `${val}`) |
| FL-15 | P2 | Several actions fail silently: saving a lead email, "Draft with AI", and others only check `status === 'success'` and log failures to the console. | `saveProfileEmailUpdates`, `generateSingleLeadPitch` |

#### Prospects, Job Leads, Billing, Settings, Mobile

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| FL-16 | P2 | "Export CSV" is active with zero leads and opens a header-only CSV in a new tab. Empty-state and inspector text is jargon ("No matching pipeline leads captured inside this view index.", "Select any lead row to engage workstation nodes."). | Rendered `/prospects` |
| FL-17 | P2 | `/billing#buy-solo` scrolls to the plans but doesn't highlight or pre-select Solo, so the intent from `/plans` is only partly kept. When payments are off, every buy button is disabled, and "Contact support" has no link. | Rendered `/billing#buy-solo` |
| FL-18 | P3 | Dates are formatted inconsistently: Billing shows "Plan expiry: 11/3/2026" while the sidebar shows "Nov 3, 2026". The disabled top-up button is labelled "Active paid plan required" instead of an action with a helper note. | Billing vs sidebar |
| FL-19 | P3 | The mobile drawer opens correctly (Escape and an outside click close it, and focus returns to ☰), but focus isn't moved into the drawer when it opens. | 390px Discovery |

### What already works well

- Session expiry: an API 401 redirects to `/login?next=<current page>`, and signing in returns there.
- `next` blocks absolute URLs (`https://example.com` → `/`); `/\example.com` was encoded and stayed on the same site (re-verify in Phase 3).
- Signed-in users visiting `/login` or `/register` are redirected into the app.
- Registration keeps the name and email on error; the duplicate-email check ignores letter case; the server enforces sources, industries, and hiring locks.
- A fully failed discovery search charged nothing, and progress shows "0 of 1 searches finished".
- Locked hiring controls for Trial are truly disabled (but see AX-02 for how they look).

## Phase 3: Backend & security

### Method

- Static review of every route in [control_gate_api.py](src/control_gate_api.py) and of [auth.py](src/auth.py), [team_api.py](src/team_api.py), [teams.py](src/teams.py), [purchase_api.py](src/purchase_api.py), [payments.py](src/payments.py), [billing.py](src/billing.py), [discovery_runs.py](src/discovery_runs.py), [user_email.py](src/user_email.py), [email_hunter.py](src/email_hunter.py), [screenshot_agent.py](src/screenshot_agent.py), [discovery_job_contacts.py](src/discovery_job_contacts.py), the lead scorer, website checker, follow-up scheduler, the templates, and `requirements.txt`.
- Live probes against the offline preview (isolated database, all outbound HTTP and SMTP blocked). The findings marked **Verified** were reproduced there.
- No secrets were read; `.env` was not opened.

### Findings

Severity for security items: **P0** exploitable now with real impact · **P1** serious weakness, fix before public launch · **P2** defense in depth · **P3** hygiene.

#### Injection & content handling

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC-01 | **P0** | **Stored XSS from scraped data.** The prospects table renders `lead.business_name` and `lead.category` through `tr.innerHTML`. Those values come from Google Maps, Yelp, Facebook, and Instagram listings, which anyone can edit. A business named `<img src=x onerror=…>` runs script in the user's session: it can read the CSRF meta token, buy plans, invite members, change the SMTP settings, or export every lead. | **Verified:** a lead named `Acme <img src=x onerror="window.__xssLead=1">` set the flag on `/prospects`. [dashboard.html](src/templates/dashboard.html) `renderTable` |
| SEC-02 | P1 | The other `innerHTML` sinks: the custom business type (FL-14) and `statusBadges[lead.status] \|\| lead.status`, which falls back to a raw status. Good: job rows, the profile modal, and the inspector use `textContent`/`innerText`, and the link setters only allow `http(s)`. | [dashboard.html](src/templates/dashboard.html) `addCustomBizType`, `renderTable` |
| SEC-03 | P1 | **CSV formula injection.** `/api/export` writes scraped values unchanged, so a name or category starting with `=`, `+`, `-`, or `@` runs as a formula when the CSV is opened in Excel or Sheets. The `status` query parameter also goes unescaped into the `Content-Disposition` filename. | [control_gate_api.py](src/control_gate_api.py) `export_leads_csv` |
| SEC-04 | P2 | **Email composition.** `/api/dispatch` doesn't validate `destination_email`. With no destination, the recipient falls back to the sender's own address, and the prospect's stored email is then **overwritten with the sender's address** (data bug). The From display name isn't escaped, and pitch text goes into the HTML body unescaped. | [control_gate_api.py](src/control_gate_api.py) dispatch route; [user_email.py](src/user_email.py) `send_email` |
| BE-01 | P1 | `send_email` strips every space and `-` from the decrypted SMTP password, so real passwords containing those characters always fail. (It was meant for Gmail app passwords only.) | [user_email.py](src/user_email.py) `send_email` |

#### Server-side requests (SSRF) & untrusted browsing

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC-05 | **P0** | **Headless Chrome runs with `--no-sandbox` on scraped URLs.** Any website listed by a business is loaded in an unsandboxed browser on the app server. One Chrome bug is then a full server compromise. The browser can also reach internal addresses (`localhost`, cloud metadata `169.254.169.254`), and the screenshot captures whatever it renders. | [screenshot_agent.py](src/screenshot_agent.py) |
| SEC-06 | P1 | **Screenshots are public.** They're saved to `static/screenshots/snap_{place_id}.png` and served by the public `static` endpoint with no login or ownership check. Place IDs are guessable from Google, so one tenant's screenshots (including any internal page from SEC-05) are visible to anyone. `.gitignore` doesn't exclude the folder. | [screenshot_agent.py](src/screenshot_agent.py); `PUBLIC_ENDPOINTS` in [auth.py](src/auth.py) |
| SEC-07 | P1 | **The email crawler fetches scraped URLs with no address check.** `attempt_domain_email_crawl` follows redirects to private and loopback addresses, and stores any email found on internal pages. It's used by Google, Yelp, and Instagram discovery. | [email_hunter.py](src/email_hunter.py) |
| SEC-08 | P1 | **SMTP settings work as an internal port scanner.** The user picks any host and port, and the test reply leaks the socket error. A port that's open answers "Connection unexpectedly closed: timed out"; a closed port answers "refused". | **Verified:** host `127.0.0.1:55439` (the database) gave a distinct reply. [user_email.py](src/user_email.py) `_connect` |
| — | Good | `discovery_job_contacts.web_url` and `_fetch_page` already reject non-global IP literals and follow redirects manually. Reuse that guard everywhere, and add a DNS resolve-and-check step so hostnames that point at private IPs are also blocked. | [discovery_job_contacts.py](src/discovery_job_contacts.py) |

#### Abuse, cost & availability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC-09 | P1 | **Unbounded OpenAI spend.** `/api/trigger/pitch-bulk` is still live although its UI was removed: it takes any `limit`, isn't charged, and starts a new thread on every call. `pitch-single` isn't charged or rate-limited either, and keeps a database transaction open during OpenAI calls of 15s or more. | [control_gate_api.py](src/control_gate_api.py) pitch routes |
| SEC-10 | P1 | **Unbounded background threads.** `score-leads`, `check-websites`, `followup-sweep`, and `pitch-bulk` each start a thread per request, with no "already running" guard. A loop of POSTs exhausts threads, database connections, and the PageSpeed quota. Discovery is guarded by worker slots; these aren't. | Trigger routes ~1520–1570 |
| SEC-11 | P1 | **`per_page` has no upper limit** in `/api/queue` and `/api/job-leads`, so one request can return a user's whole table (`per_page=100000` returned 200). | **Verified.** |
| SEC-12 | P1 | **No rate limiting anywhere**: login, register, the SMTP test, invitations, pitches, and discovery quotes. Invite and revoke can be looped to send unlimited system emails (Resend) to any address, which hurts the sender domain's reputation. | Route list; [teams.py](src/teams.py) |

#### Errors, headers & sessions

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC-13 | P1 | **The global error handler returns raw exception text with HTTP 500 for everything**, including 404 and 405. Bad input such as `page=abc` or `page=0` leaks Python and PostgreSQL messages ("invalid literal for int()", "OFFSET must not be negative"). Monitoring also logs every 404 as a "SERVER CRASH". (Extends NV-01.) | **Verified:** `/nope` → 500; `page=abc` → 500 with the ValueError text |
| SEC-14 | P1 | **No security headers.** Responses have no `Content-Security-Policy`, `X-Frame-Options`/`frame-ancestors` (clickjacking on Billing and Team), `X-Content-Type-Options`, `Referrer-Policy`, or HSTS. Third-party scripts (Tailwind browser build from jsDelivr, Leaflet from unpkg) load with no `integrity` attribute, so a CDN compromise means full XSS. The `Server: Werkzeug/…` header shows versions. | **Verified** headers on `/prospects`; [base.html](src/templates/base.html), [dashboard.html](src/templates/dashboard.html) |
| SEC-15 | P2 | **CSRF tokens are checked only on some routes**: team, purchases, paid discovery, and hiring services. These have no token check: dispatch, reject, update-email, email-settings (save, delete, test), every `/api/trigger/*`, logs clear, login, register, and logout. SameSite=Lax cookies and JSON-only bodies prevent most cross-site use today, but bodyless POSTs rely on SameSite alone, and login CSRF is possible. | **Verified:** `POST /api/reject/<id>` without a token is accepted (404 only because the id didn't exist) |
| SEC-16 | P2 | **Sessions can't be revoked.** These are signed client-side cookies valid for 14 days. Logout doesn't invalidate a stolen cookie, and there's no "sign out everywhere" or password-change revocation. Add a `session_version` column checked on every request. | [control_gate_api.py](src/control_gate_api.py) config |
| SEC-17 | P2 | **Unsafe production default.** `APP_ENV` defaults to `local`, which falls back to the development secret key and turns off Secure cookies. A production deploy that forgets the variable is silently insecure. The app should refuse to start without `FLASK_SECRET_KEY` unless it's explicitly local. | Startup log: "FLASK_SECRET_KEY is unset — using the local development key." |
| SEC-18 | P2 | Auth hardening: FL-01 (timezone lockout), FL-02 (enumeration and targeted lockout), and FL-04 (weak passwords) still apply. `safe_redirect_target` should also reject a leading `/\` explicitly instead of relying on Werkzeug encoding it. | [auth.py](src/auth.py) |

#### Data, logging & dependencies

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC-19 | P2 | **Personal data in logs.** Lead emails and business names are printed to stdout (`[DATABASE RECORD LINK] … -> email`). PostgreSQL logged a full failed `INSERT`, including the **password hash**, during a duplicate registration. Set `log_min_error_statement = panic` (or log only the error), and redact app logs. | Phase 2 database log; discovery modules |
| SEC-20 | P2 | **Outdated or unneeded dependencies.** `requests==2.31.0` is affected by CVE-2024-35195 (fixed in 2.32.x). `Flask==3.0.2` is behind 3.1. The production requirements also include desktop and build tools (`pywebview`, `pyinstaller`). Several packages are unpinned (`>=`). | [requirements.txt](requirements.txt) |
| SEC-21 | P3 | Provider cost leak: the hiring trigger response includes `max_provider_credits` (see CP-01). Error text from OpenAI is passed through to the client, along with its status code (401 when the key is missing). | Hiring trigger, pitch routes |
| BE-02 | P2 | `int()` parsing of `page`, `per_page`, and `limit` isn't validated anywhere. Use one helper that clamps to `1..N` and returns 400 for bad input. | Routes listed in SEC-11/13 |
| BE-03 | P3 | The `/api/trigger/pitch-bulk` endpoint and its server code are dead since the bulk-pitch UI was removed. Delete them rather than protect them. | — |

### What already works well

- **Tenant isolation:** every prospect and job-lead query, the scorer, the website checker, and the follow-up sweep filter by `user_id`. Discovery run logs and status are scoped to their owner through a thread-local run id, so no logs leak between accounts.
- **Billing:** reservations lock the wallet row, are idempotent (UUID key plus a payload fingerprint), allow one active run per user, enforce the daily cap in UTC, and track new leads in the same transaction as the lead insert. Settlement uses consistent lock ordering and can't settle twice. A manual `recover` CLI exists for crashed workers.
- **Payments:** HMAC-verified webhook with a 300-second window and constant-time comparison; the session is re-fetched from PayMongo; amount, currency, livemode, and refunds are checked; idempotency keys; one open order per wallet; checkout host allowlist.
- **Teams:** invitation tokens are hashed and single-use, must match the invitee's email, and seat checks run under row locks.
- **Secrets:** SMTP passwords are Fernet-encrypted with a key kept outside the database; `.env` is git-ignored and no key files are tracked; passwords are hashed.
- All SQL uses parameters; the only f-string query (`export`) builds its column list from constants.

---

## Implementation plan

Milestones are ordered by risk. **Milestone S ships first and alone**, because it closes problems that can be exploited today. All later milestones keep the numbering from Phases 1–2.

### Milestone S: Security hotfix (P0–P1, before any public launch)
S1. ✅ **Done**: every dynamic `innerHTML` value replaced with elements and `textContent`; unknown statuses show as text; custom types capped at 60 characters (input plus server `MAX_INDUSTRY_LENGTH`). Tests in `tests/workspace.test.js` and `tests/test_team_billing.py`; verified live with the same malicious lead (SEC-01, SEC-02, FL-14). **Follow-up for you:** rotate `FLASK_SECRET_KEY` on the server after this ships.
S2. ✅ **Done**: Chrome runs sandboxed (no `--no-sandbox`) behind `net_guard.FilteringProxy`; screenshots go to a private Cloudflare R2 bucket and are served by `GET /api/prospects/<id>/screenshot` (owner check, 5-minute signed redirect); `/static/screenshots/*` returns 404; capture is skipped when R2 is not configured; folder git-ignored. Tests in `tests/test_screenshot_security.py`; verified live (SEC-05, SEC-06). **Follow-ups for you:** create the R2 bucket and set `R2_*`, run `python -B src/manage_screenshots.py upload-legacy`, and purge `static/screenshots` from git history after committing.
S3. ✅ **Done**: `net_guard.safe_get` sends every crawl through the filtering proxy (IP re-checked at connect time, so DNS rebinding fails); same-site redirects only (max 3, contact lookup also applies `web_url`); 2 MB body cap; 6 s per page (8 s for contact lookup). Used by the email crawler and the company contact lookup; the website checker validates URLs before calling PageSpeed. Tests in `tests/test_safe_fetch.py`; verified live (`localtest.me`, `169.254.169.254` blocked) (SEC-07).
S4. ✅ **Done**: mail servers must be public host names (localhost, private/link-local IPs, `.local`/`.internal` names rejected on save); only ports 465, 587 and 2525 (port 25 dropped; Settings now uses a port dropdown); connections resolve, check every address and connect to the checked IP (DNS rebinding fails) while TLS still verifies the real host name. Users see short categories (not found, not allowed, timed out, secure connection failed, refused, could not connect); raw errors go to the server log only. Applies to test and outreach sends. Tests in `tests/test_user_email.py`; verified live against smtp.gmail.com (STARTTLS and SSL). Test-email rate limit moved to S9 (SEC-08).
S5. ✅ **Done**: `pitch-bulk` and dead `pitch_generator.py` removed. AI drafts cost 1 credit from the shared wallet (Solo and up; the Free Trial gets an Upgrade prompt), refunded automatically if OpenAI fails, one draft in flight per user, OpenAI is called with no database connection open, and provider errors are no longer shown to users (SEC-09, BE-03).
S6. ✅ **Done**: `score-leads`, `check-websites` and `followup-sweep` run through a per-user job guard (409 while running; the slot is released on errors). Website checks cool down for 1 hour after scoring sites (429 + Retry-After). New `GET /api/jobs/status`; the dashboard buttons stay disabled while a job runs and the queue reloads when it finishes (SEC-10).
S7. ✅ **Done**: `read_page_args` is shared by `/api/queue` and `/api/job-leads`. Non-integers return 400 `invalid_pagination` with no Python/SQL text, `page` is clamped to 1–100,000, and `per_page` to 1–50 (SEC-11, BE-02).
S8. ✅ **Done**: HTTP errors keep their real status (400/401/403/404/405/413/429), with JSON for `/api/*` and a public-layout `error.html` for pages (context-aware buttons, light/dark). Crashes return a generic 500 with a 6-character reference; the traceback and reference go only to the server log. 404/405 aren't logged (SEC-13, NV-01, Milestone A item 1).
S9. ✅ **Done**: fixed-window limits in a PostgreSQL `rate_limit_hits` table (shared across workers; keys are SHA-256 hashed, so no emails or IPs are stored). Sign-in: 8 failures per email + IP per 15 min and 20 attempts per IP per 15 min; the account-wide lockout is removed, so nobody can lock a real user out and unknown emails behave the same (FL-02). Sign-up 5/IP/hour, SMTP test 5/user/10 min, outreach sends 20/user/hour, AI pitches 10/user/min, quotes 60/user/min, and team invitations 20/team/day (revoked ones count). APIs return 429 `rate_limited` with `Retry-After`; login and register re-render the form with the message. `TRUSTED_PROXY_HOPS` turns on ProxyFix (set it to 1 on Render/Railway/Heroku/Nginx). Tests in `tests/test_rate_limit.py` and the DB tests (SEC-12, SEC-08, FL-02).
S10. ✅ **Done**: export cells that start with `= + - @`, tab or CR get a `'` prefix, but plain phone/number values (e.g. `+63 912 345 6789`, `-3`) are kept. `?status=` must be one of the 7 known statuses (otherwise 400 `invalid_status`, checked before any query). The filename is built by the server and quoted (`leads_<status|all>_<YYYY-MM-DD>.csv`), with a UTF-8 BOM for Excel, `no-store` and `nosniff`. Tests in `tests/test_dashboard_pages.py` (SEC-03).
S11. ✅ **Done (resolved by S9)**: the account lockout that read `locked_until` is gone, and the rate-limit table uses `TIMESTAMPTZ` with Unix-epoch windows, so the database time zone no longer matters. The unused `failed_login_count`/`locked_until` columns were removed from `schema.sql` and the startup `CREATE TABLE` for new installs; existing databases keep them untouched (harmless) (FL-01).

### Milestone T: Hardening (P2)
T1. ✅ Done. `src/security_headers.py` sets an enforced CSP (only cartocdn tiles, Photon and R2 images allowed from outside; `'unsafe-inline'` kept for the inline handlers), a strict report-only CSP that reports to `/api/csp-report` (deduped, rate-limited, logged), `nosniff`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, Referrer-Policy, Permissions-Policy, COOP, and HSTS (1 year, this host only, HTTPS outside local mode). Leaflet, Tailwind browser 4.3.3 and the fonts are self-hosted under `static/vendor/` (SEC-14). `gunicorn.conf.py` hides the server version and runs one worker. Follow-up: once the inline `on*=` handlers and the inline dashboard script are moved to files, enforce the report-only policy.
T2. ✅ Done. `src/csrf.py` runs one app-wide `before_request` check (after the sign-in guard) on every POST/PUT/PATCH/DELETE: the session token must come in `X-CSRF-Token` or a hidden `csrf_token` field (added to login, register and logout), and a cross-site `Origin`/`Referer` is rejected even with a token (`APP_BASE_URL` host also trusted). The per-module checks in team_api, purchase_api and the hiring-service route were removed. Expired auth forms re-render with the email kept; a stale logout keeps the user signed in. Exempt: PayMongo webhook and CSP reports. Unmatched routes still return 404/405 (SEC-15).
T3. ✅ Done. `users.session_version` (startup ALTER for existing databases, plus schema.sql) is stored in the session at sign-in and compared on every request by the existing user lookup; a mismatch signs the session out. Sessions from before this change are adopted only while the account's version is still 1. Settings → Account security adds Change password (5 attempts per 15 min; other devices are signed out, this one stays) and Sign out of all devices. The forgot-password reset was moved to T9 (SEC-16, FL-03).
T4. Refuse to start without `FLASK_SECRET_KEY` unless `APP_ENV` is explicitly `local` or `desktop`; document `APP_ENV=production` (SEC-17). ✅ Done: unset APP_ENV = production; allowed values local/desktop/production/staging; desktop launcher forces desktop; key-strength check skipped by choice.
T5. Validate `destination_email`; never overwrite a prospect's email with the sender's address; escape the display name and the HTML body; stop removing spaces and dashes from SMTP passwords (SEC-04, BE-01; re-save existing settings needs no migration). ✅ Done: blank/invalid recipient rejected (400 invalid_recipient), no sender fallback; recipient saved after send; From via formataddr, subject newlines collapsed; pitch HTML-escaped; Gmail app-password spaces removed only for smtp.gmail.com, others used verbatim; label renamed to "Recipient email".
T6. Redact emails in app logs, set PostgreSQL `log_min_error_statement`, and remove provider cost and OpenAI error pass-through from responses (SEC-19, SEC-21). ✅ Done: log_redact masks emails (o***@shop.com) on stdout/stderr and all log records incl. tracebacks; user's live log keeps full emails; duplicate sign-up uses ON CONFLICT DO NOTHING (verified PostgreSQL logs nothing); README documents log_min_error_statement=panic; max_provider_credits and max_job_requests removed; OpenAI pass-through was already fixed.
T7. Upgrade `requests` to 2.32.x or later and Flask to 3.1.x, pin all versions, and move `pywebview`/`pyinstaller`/`selenium` into a separate desktop requirements file (SEC-20). ✅ Done: all deps pinned with == (Werkzeug 3.1.9 fixes PYSEC-2026-4312, requests 2.34.2), pip-audit clean, desktop deps in requirements-desktop.txt, selenium kept (server screenshots), .python-version 3.12, 333 tests pass on 3.12 and 3.14.
T8. Explicitly reject `/\` in `safe_redirect_target`; generic auth messages (SEC-18, FL-02, item 10e). ✅ Done: safe_redirect_target rejects any backslash (also %5C), whitespace/control chars, and anything with a scheme or host; sign-up conflict now says "We couldn't create an account with that email. If it's yours, sign in instead." (full fix with email verification in T9); deactivated message kept (only shown after the correct password). Tests in test_auth_isolation, test_csrf, test_log_redact; verified live.
T9. The IP-based free-trial protection plan (already agreed), together with email verification. Also add the forgot-password email reset here (moved from T3); a reset must raise `session_version`. ✅ Done: email verification (trial at verification), forgot/reset password, one trial per network (HMAC), hosted startup checks.

### Rollout order
1. **Milestone S** in one release, with a regression test for each item. Then rotate `FLASK_SECRET_KEY` (signs everyone out once) in case a session was stolen through SEC-01.
2. **Milestone A** (trust, legal, broken paths) and **Milestone T**.
3. **Milestone B** (conversion and navigation).
4. **Milestones C and D** (visual system, accessibility, copy).


### Milestone A: Trust, legal & broken paths (P0)
1. ✅ **Done in S8.** Add proper 404/405/500 handlers: an HTML error page for browsers and JSON for `/api/*`, with no exception text in responses (NV-01).
2. Add Terms, Privacy, Refund Policy, and Contact/Support pages, and link them from every footer, the Billing page, and the registration page (NV-02).
3. Remove provider names, provider request counts, and provider credit costs from customer UI; describe cost only in site credits (CP-01).
4. Mark unreleased features as "Coming soon" in the pricing table, or remove them (CP-02).
5a. ✅ **Done in S9/S11.** Store and compare `locked_until` as `timestamptz`, with a migration (FL-01).
5b. Fix the invitation hand-off: keep the token through sign-in and registration (for example, in `sessionStorage`, or move it to a one-time server-side lookup), disable Accept when there's no token, and use the public layout when signed out (FL-07, FL-08, FL-09).
5c. Add password reset by email and a change-password page; send email verification together with the IP-based trial plan (FL-03).

### Milestone B: Conversion & navigation (P1)
5. Preserve purchase intent: send anonymous plan CTAs to `/register?next=/billing?plan=solo`, pass `next` between login and register, and pre-select the plan on Billing (NV-03, PR-04).
6. Render styled upgrade/permission pages for 403s on HTML routes, and point upgrade links to `/billing` (NV-04, NV-05).
7. Show a live search cost next to the discovery submit button; limit pages-per-role options by plan (PR-02, PR-03).
8. Generate the public pricing table from `plans.py`/`payments.PRICES` (PR-01).
9. Make copy plan-aware: say "your credits" for Trial and Solo, "shared credits" for teams; fix the "1 industries" grammar (CP-06, CP-07).
10. Update home copy for 5 sources, LinkedIn Jobs, and the free-trial CTA (CP-05).
10a. Carry `next` between login and register, and pre-select and highlight the plan on Billing (FL-06, FL-17).
10b. Show plan limits in the discovery form (industry counter, disable extra boxes); no default industry; resolve the location before the confirm, with a busy state; replace `confirm()` with an in-page cost summary (FL-10–FL-12, PR-02).
10c. Clear outcome messages: say "no credits charged" when nothing was charged, hide internal log phrasing, and show visible errors for every failed action (FL-13, FL-15).
10d. Escape all user and scraped values inserted with `innerHTML`, starting with the custom business type (FL-14; full sweep in Phase 3).
10e. Use generic auth messages for lockout and registration conflicts, plus per-IP and per-account rate limiting (FL-02; details in Phase 3).

### Milestone C: Visual system & accessibility (P1–P2)
11. Define a type scale (12 / 13 / 14 / 16 / 18 / 22 / 28 / 36 / clamp hero), raise all text to at least 12px and buttons and nav to at least 13–14px, and remove one-off sizes (UI-01, UI-04).
12. Darken light-theme `--subtle` (about `#6b7a83`, ≥4.5:1) and `--muted` (about `#5a6c76`) (UI-02).
13. Replace the runtime Tailwind CDN with token-based component classes (or a build-time Tailwind compile), and remove the attribute-selector remapping (UI-03).
14. Self-host or `preconnect`/`preload` fonts and reduce weights to 3 per family (UI-05).
15. Build one accessible modal helper (dialog role, Escape, focus trap and return, labeled close) for all modals (AX-01).
16. Add a clear locked-state style (dimmed panel, lock badge, prominent upgrade CTA) and relayout locked source chips (AX-02).
17. Label all inputs, name the pagination buttons, and move inline `onclick` handlers to listeners (AX-03, AX-04).
18. Restore focus outlines on inputs (AX-05); add search and select-all to the industry picker (AX-06).

### Milestone D: Copy polish & consistency (P2–P3)
19. Rewrite jargon strings (CP-03) and spec-style paragraphs (CP-04); move long help text into collapsible "How this works" sections (CP-08).
20. Replace emoji icons with the SVG icon set; clarify "Approve & Send Test" (CP-09, CP-10).
21. Format credits and dates with `Intl`, use `…`, and standardize capitalization (UI-07, UI-08, CP-11).
22. Simplify the header (remove duplicate brand tag and status, page-specific CTAs) and add an account menu with email and password change (NV-06–NV-09).
23. Add `theme-color` meta and select styling; remove `autofocus` on mobile (UI-06, AX-07).
24. Show field-level registration errors with `aria-invalid` and focus; add a password strength and common-password check (FL-04, FL-05).
25. Disable Export when there are no leads; one date format everywhere; move focus into the mobile drawer (FL-16, FL-18, FL-19).

### Verification for every milestone
- Existing Python and JS suites, plus new UI tests for the error pages, the purchase-intent redirect, and the modal helper.
- Automated contrast and text-size check (minimum 12px, AA contrast) at 390 and 1440 widths in light and dark.
- Manual keyboard-only pass through register → discovery → billing.
- Security regression tests: XSS payload lead names; SSRF to `127.0.0.1`, `169.254.169.254`, and a hostname that resolves to a private IP; out-of-range `per_page`/`page`; CSV formula cells; missing CSRF token on every POST; rate-limit responses (429); 404/405 return the right status with no exception text; required headers present.
- Before launch: `SHOW timezone` on the production database, confirm `APP_ENV` and `FLASK_SECRET_KEY` are set, and run `pip-audit` on the pinned requirements.

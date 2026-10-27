# lead_gen

## Accounts and data isolation

Workspace pages and API routes require a signed-in account. The product home,
sign-in, and registration pages are public. Visiting a workspace page while
signed out redirects to `/login`; API calls return `401` with
`{"code": "auth_required"}` so the dashboard can send you back to sign in
instead of failing on an unexpected HTML response.

| Route | Purpose |
| --- | --- |
| `/` | Public product home; Overview for signed-in accounts |
| `/overview` | Signed-in Overview |
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

### Dependencies

The app targets **Python 3.12** (declared in `.python-version`; 3.14 also
passes the test suite). Every dependency is pinned with `==`:

| File | Use |
| --- | --- |
| `requirements.txt` | Web server (Flask, gunicorn, discovery, billing, email) |
| `requirements-desktop.txt` | Desktop launcher and `.exe` build: the server set plus pywebview and PyInstaller. `build.bat` installs it. |

```powershell
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements-desktop.txt   # or requirements.txt on a server
```

To upgrade, change one pin, run the test suite, then audit for known
vulnerabilities in a throwaway environment:

```powershell
python -m venv $env:TEMP\audit; & $env:TEMP\audit\Scripts\pip install pip-audit
& $env:TEMP\audit\Scripts\pip-audit -r requirements.txt -r requirements-desktop.txt
```

gunicorn is not installed on Windows (`sys_platform != "win32"`), so a Windows
audit skips it; audits run on Linux or in CI include it.

### First-time setup

```powershell
venv\Scripts\python.exe app.py           # start once so migrations create the tables
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
| `FLASK_SECRET_KEY` | When `APP_ENV` is `production` or `staging` (or unset) | Signs session cookies. Keep it stable — changing it signs everyone out. `local`/`desktop` fall back to a public development key |
| `SMTP_ENCRYPTION_KEY` | To send mail | Encrypts each account's stored SMTP password |
| `APP_ENV` | Yes for development (unset = `production`) | One of `local`, `desktop`, `production`, `staging`; any other value stops startup. `local`/`desktop` allow plain HTTP cookies, skip HSTS and allow the development key. `production`/`staging` require HTTPS and `FLASK_SECRET_KEY`. The desktop launcher (`app.py`) always sets `desktop` |
| `FLASK_DEBUG` | No (off) | Debug is opt-in; the Werkzeug debugger is a remote code execution path |
| `MAX_DISCOVERY_WORKERS` | No (`4`) | Discovery searches running at once across all accounts (clamped to 1-32) |
| `APP_HOST` / `APP_PORT` | No | Bind address, default `127.0.0.1:5000` |
| `TRIAL_IP_HASH_SECRET` | When `APP_ENV` is `production` or `staging` (or unset) | Key for the one-trial-per-network rule (see *Email verification and free trials*). Keep it stable; changing it forgets which networks already used a trial |
| `SYSTEM_SMTP_PASSWORD` / `SYSTEM_SMTP_FROM` / `APP_BASE_URL` | When `APP_ENV` is `production` or `staging` (or unset) | System mail for verification, password reset and team invitations (see *Sending email*). Hosted deployments refuse to start without them |
| `TRUSTED_PROXY_HOPS` | Behind a proxy (`0`) | Number of proxies in front of the app (Render, Railway, Heroku or Nginx: `1`). Needed so rate limits see the real client IP; never set it higher than the real number of proxies, or clients can fake their IP |

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"                      # FLASK_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"                      # TRIAL_IP_HASH_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # SMTP_ENCRYPTION_KEY
```

Passwords are hashed with Werkzeug (scrypt). Sign-in failures return one
generic message whether or not the email exists. Accounts are never locked;
instead, rate limits (stored in PostgreSQL, so they hold across workers) apply:

| Action | Limit |
| --- | --- |
| Sign-in failures | 8 per email + network per 15 min |
| Sign-in attempts | 20 per network per 15 min |
| Sign-up attempts | 5 per network per hour |
| SMTP test email | 5 per account per 10 min |
| Outreach sends | 20 per account per hour |
| AI pitch drafts | 10 per account per minute |
| Discovery quotes | 60 per account per minute |
| Password change attempts | 5 per account per 15 min |
| Team invitations | 20 per team per day (revoked ones count) |
| Resend verification email | 3 per account per hour |
| Verify-email link submissions | 30 per network per hour |
| Forgot-password requests | 5 per network and 3 per email per hour |
| Reset-password submissions | 10 per network per 15 min |

Limits use fixed windows, so a block can end sooner than the full window.
APIs answer `429` with code `rate_limited` and a `Retry-After` header.

### Sessions and account security

Sign-in sessions last 14 days. Each session stores the account's
`users.session_version`, and every request compares it with the database, so
raising the version signs out every existing session at once. In
**Settings → Account security**:

- **Change password** (current + new password, 5 attempts per 15 minutes)
  signs out every other device; this browser stays signed in.
- **Sign out of all devices** signs out everywhere, including this browser.

- **Forgot password?** on the sign-in page emails a reset link (1 hour,
  single use; requesting another link cancels the previous one). Resetting
  signs out every device, including this browser, and returns to sign-in.

Sessions created before this feature are adopted on their next request, as
long as the account has never revoked its sessions.

Sign-in, sign-up and forgot-password messages do not reveal whether an email
is registered. A wrong password and an unknown email get the same message; the
forgot-password page always answers "If an account exists for that email..."
and sends the email in the background so timing doesn't differ. Signing up
with a taken email says "We couldn't create an account with that email. If
it's yours, sign in or reset your password."
The `next=` link after sign-in only accepts same-site paths. Anything with a
scheme, a host, `//`, a backslash (`\` or `%5C`), whitespace, or control
characters falls back to the default page.

### Production server

```bash
APP_ENV=production FLASK_SECRET_KEY=... gunicorn -c gunicorn.conf.py --chdir src control_gate_api:app
```

Set `APP_ENV=production` (or `staging`) and `FLASK_SECRET_KEY` on the host.
Without a key the app refuses to start rather than run with the development
key. For local development put `APP_ENV=local` in `.env`.

`gunicorn.conf.py` binds `0.0.0.0:$PORT` and runs **one worker** with
`GUNICORN_THREADS` threads (default 8). Keep a single worker: discovery runs
and job guards live in process memory. The config also hides the gunicorn
version from the `Server` header.

### Security headers

`src/security_headers.py` adds these headers to every response, including
errors and static files:

- `Content-Security-Policy`: everything is served from this site except map
  tiles (`basemaps.cartocdn.com`), place search (`photon.komoot.io`) and
  screenshot images (`*.r2.cloudflarestorage.com`). Inline scripts and styles
  are still allowed, because the templates use inline `on*=` handlers.
- `Content-Security-Policy-Report-Only`: the same policy without inline
  scripts. Browsers report what it would block to `POST /api/csp-report`, and
  each distinct violation is logged once as `CSP violation: ...`. When the
  log is clean, the strict policy can be enforced.
- `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy` and `Cross-Origin-Opener-Policy`.
- `Strict-Transport-Security` (1 year, this host only), sent only over HTTPS
  and never when `APP_ENV` is `local` or `desktop`.

A route can set its own value for any of these headers, and that value is
kept. If you add a new external host, add it to the policy in
`security_headers.py`.

### CSRF protection

`src/csrf.py` checks every POST, PUT, PATCH and DELETE request in one place,
so a new route is protected without extra code:

- The session token must arrive in the `X-CSRF-Token` header (the fetch
  wrapper in `static/workspace.js` adds it to every `/api` call) or in a
  hidden `csrf_token` form field (login, register and logout forms). The
  token changes at sign-in and sign-out.
- A request whose `Origin` or `Referer` names another site is rejected even
  with a valid token. The request's own host is always allowed; behind a
  proxy that rewrites `Host`, set `APP_BASE_URL` so the public host is
  allowed too.
- Failures: `/api` calls get JSON `403` with code `csrf_failed`; an expired
  login or register form is shown again with "Your page expired. Please try
  again." and the email kept; a stale logout leaves the user signed in and
  returns to the overview.
- Exempt: the PayMongo webhook (it verifies its signature) and
  `/api/csp-report` (browsers cannot attach tokens, and it only logs).

### Logs and personal data

`src/log_redact.py` masks email addresses in everything the app writes to
stdout and stderr, and in every log record (including tracebacks), as
`o***@shop.com`: the first character and the domain stay for debugging.
The live discovery log a user sees on screen keeps the full address;
only the server copy is masked. Business names are not masked; they are
public listing data.

Signing up with an email that already exists is handled with
`INSERT … ON CONFLICT DO NOTHING`, so PostgreSQL never logs a failed
`INSERT` carrying a password hash. Other failed statements can still contain
personal data, so on the database server also set:

```sql
ALTER SYSTEM SET log_min_error_statement = 'panic';  -- log the error, not the statement
SELECT pg_reload_conf();
```

This needs a superuser, so the app cannot set it itself. On managed
PostgreSQL (Render, Railway, Supabase, RDS), set the same parameter in the
provider's database settings.

API responses never include provider request counts or costs. AI and mail
failures return a generic message; the details stay in the server log.

### Vendored front-end files

Third-party browser files are self-hosted under `static/vendor/`, each folder
with its license:

| Folder | Source |
| --- | --- |
| `leaflet-1.9.4/` | `leaflet` npm package (`dist/`) |
| `tailwindcss-browser-4.3.3/` | `@tailwindcss/browser` npm package (`dist/index.global.js`, renamed `tailwind.js`) |
| `fonts/` | `@fontsource-variable/dm-sans` and `@fontsource-variable/manrope` 5.3.0 (latin and latin-ext woff2) |

To upgrade one, download the new package with `npm.cmd pack <name>@<version>`,
copy the files into a new versioned folder, update the template URLs, and
run `tests/test_security_headers.py` (it fails if a template loads anything
from another site).

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

Providers using two-factor authentication require an **app password**. For
`smtp.gmail.com`, a password in Gmail's app-password format
(`abcd efgh ijkl mnop`) has its spaces removed. Every other password is used
exactly as saved, including spaces and dashes.

**Approve & Send** (`POST /api/dispatch/<id>`) needs a valid address in the
**Recipient email** box. A blank or invalid address returns `400` with
`{"code": "invalid_recipient"}`, and nothing is sent; there is no fallback to
your own address. After a successful send the address is saved as the
prospect's email. The pitch is plain text: it is HTML-escaped and line breaks
become `<br>`. The sender name is encoded into a standard `From` header, and
line breaks in the subject are collapsed, so neither can add extra headers.

## Workspace

Visitors at `/` see the public product home page, with links to sign in or
create an account. Its Light / Dark / System appearance control shares the
workspace's saved theme preference. The hero globe is illustrative, not live
account data or a claim of provider coverage. Signed-in users at `/` still see their
Overview; `/overview` also opens it directly and requires authentication.

The public navigation links to a separate **Plans & Pricing** page at `/plans`,
available with or without signing in and using the same saved theme preference.
It displays a 7-day free trial (30 credits) and Solo (PHP 999/month), Small Team
(PHP 2,499/month, Best Value), and Enterprise (PHP 5,999/month). Annual totals
include two months free; the comparison table lists credits, seats, daily search
caps, channels, rollover, and subscriber-only top-up packs. Monthly equivalents
and per-industry prices are rounded. Credits now mean **search credits**, not
delivered leads or Scrapingdog API credits. Local discovery costs 10 credits per
industry; Trial/Solo/Small Team/Enterprise allow 1/3/5/10 industries per submission.
Their 30/750/2,500/7,500 credits cover 3/75/250/750 local industry searches before
refunds when used only for local discovery. Hiring is available on Small Team
and Enterprise at 15 credits per role-page across Google Jobs and LinkedIn Jobs,
including company-contact lookups, with depth limits of 3 and 5 respectively.
Both discovery modes use one balance. Daily caps count accepted submissions.
Completed submissions producing zero new leads receive a 70% search-credit
refund (30% retained); this applies to the whole submission, not its individual
empty industries or sources. Validation failures and wholly failed provider
searches are not charged. Fractional refunds are intentional: 15 credits earns
10.5 back. Prices, allowances, seats, rollover, and top-up amounts are unchanged.
Owners can buy plans and top-ups through PayMongo QR Ph hosted checkout when
configured. Verified payments activate purchases automatically; a new account
starts its 7-day free trial with 30 credits when it verifies its email (one
trial per network; see below). Discovery enforcement,
shared team wallets, and payment notifications are implemented. Recurring
auto-debits, self-service paid-plan changes/proration, API export, customer
export webhooks, bulk CSV enrichment, and Ad Pixel Check are not implemented.

#### Discovery access enforcement

Solo local discovery allows **Google Maps and Yelp only**. Facebook,
Instagram, and LinkedIn company-page options remain visible but locked
with an **Upgrade to Small Team** prompt. Hiring Companies discovery will also
remain visible but locked for Solo with that upgrade prompt.

Small Team and Enterprise unlock all five local sources and Hiring
Companies discovery. Small Team uses preset hiring services only; its
**Add Service** control stays visible but locked with an **Upgrade to
Enterprise** prompt. Enterprise allows custom service creation and searches.
The trial's published Google Maps access is unchanged.

`src/plans.py` owns server-side capabilities and search prices. Locked sources,
Solo hiring runs, excessive industry/depth selections, and non-Enterprise
custom-service creation/selection are rejected before reserving credits or
calling providers, including custom IDs saved before a downgrade. Existing
leads remain readable when a plan is unassigned or expired.

#### User Management and shared owner credits

`/user-management` and its sidebar item are available only to active Small Team
and Enterprise owners. The server enforces ownership on every team operation;
regular members, Solo, and Trial cannot use owner actions. Members share their
owner's wallet and UTC daily submission cap, **not leads, custom service
definitions, or outgoing mailbox settings**. Removal restores the member to
an unassigned private account without deleting their leads.

The registered main account is the team owner and occupies one seat, leaving
up to 3 additional members on Small Team or 14 on Enterprise. Pending email
invitations reserve seats for 7 days; acceptance converts the reservation into
membership, and revocation/expiry releases it. Tokens are hashed in PostgreSQL,
single-use, email-bound, and carried in a URL fragment rather than server access
logs. Recipients sign in/register with the invited address. Unassigned zero-balance personal accounts and personal trial accounts can join;
joining closes the trial and discards its remaining credits without transferring
them into the team wallet. Existing paid subscriptions/teams must be
resolved first. Downgrades with excess members/invitations are rejected.

System mail (team invitations, email verification and password resets) uses
**Resend SMTP over TLS**, separate from user outreach:

```dotenv
APP_BASE_URL=https://your-domain.example
SYSTEM_SMTP_FROM=team@your-verified-resend-domain.example
SYSTEM_SMTP_PASSWORD=<your-resend-api-key>
```

The host is `smtp.resend.com`, port 465, username `resend`. Verify the sender
domain in Resend. Do not commit credentials. Missing configuration blocks
invitations explicitly; delivery failures revoke the invitation and release
its seat. No live mail is sent in tests.

#### Billing and PayMongo QR Ph purchases

`/billing` is an authenticated **owner-only** page with monthly/annual plans,
active-plan credit packs, and the latest 25 purchase records. Members cannot
read owner payment history or initiate/check/cancel purchases. Public plan
cards and the owner's sidebar credit card link to Billing.

The checkout uses **one-time QR Ph payments**, not recurring subscriptions.
Prices are server-owned PHP centavos in `src/payments.py`, matching the public
offering. The merchant absorbs PayMongo fees (`pass_on_fees=false`); checkout
does not add fees to published prices. Only `qrph` is requested. Enable QR Ph
on the merchant account before testing.

Configure on the server (never in browser code or source control):

```dotenv
APP_BASE_URL=https://your-domain.example
PAYMONGO_SECRET_KEY=<your-paymongo-secret-api-key>
PAYMONGO_WEBHOOK_SECRET=<the-signing-secret-for-this-webhook>
```

In PayMongo **Developers → Webhooks**, register:

```text
https://your-domain.example/api/paymongo/webhook
Event: checkout_session.payment.paid
```

The signing secret is the endpoint's webhook secret, **not** the API secret.
Use matching test/live credentials and webhook mode. Start with `sk_test_`
in an **isolated staging database**; test confirmations really grant access
in the configured app database, though they do not collect real money.
Never use test credentials against your production database. After completing
a sandbox QR Ph payment and observing activation, switch to live credentials
and a live webhook on the production environment.

Restart after deployment to apply `billing.SCHEMA` and `payments.SCHEMA`.
The app must be publicly reachable over HTTPS for notifications, with the
proxy forwarding this endpoint unchanged. Payment return URLs use only the
configured HTTPS origin, never the incoming Host header.

Checkout creation uses `/v2/checkout_sessions`; retrieval and cancellation use
PayMongo's documented `/v1/checkout_sessions/{id}` and `/{id}/expire`.
Only the backend sees the secret key. The public webhook is exempt from
session/CSRF checks **only for this endpoint**; it verifies the raw-body
HMAC-SHA256 signature, timestamp (5-minute tolerance), and test/live mode.
It also retrieves the session via authenticated PayMongo API before granting.
Both the line-item total and paid amount/currency/mode/reference must match.
The success/cancel URL and browser-provided prices can never activate access.

Wallet locks, unique payment/session references, a single open purchase per
wallet, and unique ledger events prevent duplicate fulfillment. Duplicate
notifications/rechecks are safe. The UI confirms the amount before redirecting
and retains a replay key across ambiguous responses.

Paid plans activate immediately on verified payment. Upgrading a free trial
replaces unused trial credits rather than carrying them into the paid grant.
Annual purchases grant one monthly allowance initially, then refresh monthly
with capped rollover. Monthly purchases cover one calendar month. Renewal is
manual after expiry, with the new period starting when payment is confirmed;
there are **no automatic future charges**. Active paid owners can buy top-ups
but must contact support for plan changes; there is no automated proration.
Top-ups bought while active are honored if confirmation arrives after expiry,
but expired access still requires renewal and normal rollover caps apply.

Plan checkout creation waits for all wallet discovery runs to settle. While a
plan purchase is open, new discovery, manual plan overrides, and joining another
team are blocked so a delayed payment cannot unexpectedly replace active work.
Top-up checkout does not pause discovery. Closing checkout or returning via its
cancel URL does not cancel the payment request; **Cancel** must first expire the
session at PayMongo. If a transfer is processing, cancellation may be refused:
recheck the order instead of paying again.

#### Payment reconciliation and operational limits

History **Recheck** retrieves the session and safely fulfills a confirmed paid
order if a webhook was missed. A checkout POST timeout is deliberately not
retried: PayMongo may already have created a payable session. Such orders
remain `unknown` (a process interruption may leave `creating`), block another
purchase, and show the order reference for support. Search PayMongo by that
reference, find the actual `cs_...` session, then run:

```powershell
python src\manage_payments.py reconcile <order-uuid> --session-id cs_actualsession
python src\manage_payments.py reconcile <order-uuid>
python src\manage_payments.py reconcile <order-uuid> --expire
```

Reconciliation still validates the server quote, reference, paid status, mode
and currency; it never grants credits solely from operator input. For an
unconfirmed creation, **only after independently verifying in PayMongo that
no payable or paid session exists**, release its local slot:

```powershell
python src\manage_payments.py close-uncreated <order-uuid> --verified-no-payable-session
```

Never use this command to dismiss a real/unknown payment without checking.
Monitor failed webhook deliveries and `creating`/`unknown`/`review` orders.
PayMongo does not replay missed events after re-enabling a disabled webhook;
reconcile affected orders. `review` records represent paid purchases that
conflict with wallet/seat state: do not ask the customer to pay again. Resolve
the entitlement or refund with support after inspecting the ledger.
Cash refunds, disputes/chargebacks, invoices/tax receipts, and corresponding
entitlement adjustments are **operator-managed**, not automated by this release.
No cash-refund endpoint is exposed. Discovery's 70% credit refund is unrelated.
There is no recurring worker or automatic financial reconciliation scheduler.

Official references: [Hosted Checkout quick start](https://docs.paymongo.com/docs/payment-channels-hosted-checkout-quick-start),
[signature verification](https://docs.paymongo.com/docs/developer-tools-webhook-setup-management),
[session retrieval](https://docs.paymongo.com/reference/get_checkout_sessions),
and [session expiry](https://docs.paymongo.com/reference/create_checkout_sessions_id_expire).

#### Email verification and free trials

New accounts are signed in right away but start **unverified**. A banner
("Verify your email to start your free trial", with **Resend email**) shows
until the link in the verification email is opened. The link lasts 24 hours,
works once and carries its token in the URL fragment (`/verify-email#...`), so
it never reaches access logs and mail scanners that prefetch links can't use
it. Verifying does not sign anyone in. A password reset link also counts as
verification. Accounts that existed before this feature were marked verified
by the migration; invited team members are verified when they accept.

The free trial starts at verification, **once per network**: the first
verified account from an IPv4 address (or IPv6 /64) gets it; later accounts
from the same network are still created but are asked to choose a plan. The
network is stored only as an HMAC keyed by `TRIAL_IP_HASH_SECRET`, never as a
raw IP address, and the claim is permanent. Shared networks (offices, schools,
and mobile carriers using CGNAT) may deny genuine users; support can still
grant a trial with `python src/manage_plans.py activate <email> trial`. Local
and desktop runs skip the network rule, since every user there arrives from
`127.0.0.1`.

Without system mail on `local`/`desktop`, verification and reset emails are
printed to the server console between `[DEV MAIL]` markers instead of being
sent. Desktop builds run without a console (`--noconsole`), so configure system
mail there if users need these links. Hosted deployments refuse to start
without system mail and `TRIAL_IP_HASH_SECRET`.

#### Manual plan overrides and credit operations

Restart after deployment to apply `billing.SCHEMA` through startup migrations.
Back up production first. New registrations receive a 7-day, 30-credit trial
when they verify their email, unless their sign-up network already used one.
Existing unassigned owners with no previous
trial/activation can select **Start free trial** in Billing, without configuring
PayMongo or making a payment. Trial claims are locked and granted only once;
expired trials and removed team members cannot restart a consumed trial.
Paid plans are activated by a confirmed purchase or operator; no existing leads are moved or
deleted. These commands are support overrides, not a way to bypass unresolved
checkouts. From the project root:

```powershell
python src\manage_plans.py activate owner@example.com small_team
python src\manage_plans.py activate owner@example.com enterprise --months 12
python src\manage_plans.py change-plan owner@example.com enterprise
python src\manage_plans.py top-up owner@example.com --payment-reference confirmed-receipt-123
```

Run only the applicable activation command for a given account, not both.
Activation accepts `trial`, `solo`, `small_team`, or `enterprise`; trial is
7 days and may be granted once per owner. Paid periods are 1 or 12 calendar
months; annual plans grant credits monthly, not 12 months upfront. Renewals
and annual monthly grants cap unused balance (including top-ups/refunds) at
the plan rollover allowance before adding the next grant. Annual grants
advance lazily on the next wallet access. Active plan changes retain balance
and billing dates, do not issue another grant, and are ledger-audited.
After a period expires, manually activate the renewed period after confirming
payment. Top-ups are the plan's published pack, require an active paid owner,
and are idempotent by payment reference. There is no public credit-grant API.

Discovery requires a CSRF token and a UUID `Idempotency-Key`. The UI shows a
server quote and confirmation before reserving credits. Wallet row locks
prevent concurrent members from overspending; one unsettled discovery per
user is allowed across both modes. Replaying a request key returns the same
run without another charge or worker. Charges/refunds are in decimal credits.
The team daily cap counts accepted submissions (including later provider
failures), but failed worker launches release their daily slot.

Lead insert counts are recorded in the reservation's transaction: upsert
updates are **not** new leads. A run with new leads pays the quoted amount;
a run with some successful discovery but zero new leads gets a 70% refund;
a wholly failed run gets a full refund. Partial failures with new leads do
not reduce the quote. All refunds return to the same owner wallet. Durable
run snapshots and the credit ledger survive application restarts.

The sidebar shows the live shared balance and cycle usage, refreshed every
30 seconds while visible, on returning to the tab, and after discovery settles.
Usage includes current reservations and subtracts refunds. Its percentage is
net cycle spending divided by that spending plus the available balance, so
rollover and top-ups are included rather than mistaken for negative usage.
The date distinguishes an annual plan's monthly credit refresh from subscription
expiry (manual renewal required). Failed refreshes explicitly mark values as stale.

Interrupted runs retain reservations rather than automatically retrying paid
requests or issuing potentially incorrect refunds. **Stop all discovery
workers first**, inspect `paid_discovery_runs` and saved results, then reconcile
each interrupted UUID:

```powershell
python src\manage_plans.py recover <run-uuid> --workers-stopped
```

Recovery charges the quote if new leads were committed, otherwise releases the
reservation fully; repeated reconciliation is safe. At billing boundaries,
new monthly grants wait until outstanding wallet reservations are settled.
This initial release uses background threads, not a durable external job queue.

Unit/UI regressions use offline fixtures. To exercise transactional tests on a
**disposable PostgreSQL server**, set `TEST_BILLING_DSN` and run:

```powershell
$env:TEST_BILLING_DSN = 'host=127.0.0.1 port=55439 dbname=postgres user=team_test'
python -B -m unittest discover -s tests -p "test_team_billing.py"
```

The tests create/drop only their uniquely named schema; they never use the
application `DATABASE_URL` for integration testing.

### Home hero globe

The public hero uses a self-hosted Three.js globe with GSAP easing. Ten featured
countries (Philippines, India, Australia, Brazil, South Africa, Japan, United
Kingdom, United States, Egypt, Indonesia) independently turn green with a
ragged paper-burn edge as they face the viewer. Real SVG flag badges appear
as burns finish, fade at the horizon, and rotate with the globe. After all
countries finish, the result holds, fades to slate, and repeats. This is a
decorative animation: it does not call discovery APIs or show lead counts.

The existing light/dark/system preference updates the globe palette.
The animation pauses offscreen, in background tabs, or with the Pause globe
button. Reduced-motion users get a completed, non-rotating globe. If WebGL
or an asset fails, the static PNG remains visible and an error is logged.
Mobile initialization uses 2048px masks, a 64-segment sphere, and six countries;
desktop uses 4096px masks and ten countries.

Built assets are included in `static/globe`, so the Flask VPS needs **no Node.js
runtime or frontend build**. Deploy these files together with the updated
template and stylesheet. CloudPanel must forward `/static/` to Flask (your
production application port is 8090), including `.js`, `.json`, `.png`, and
`.svg` files.

To rebuild after changing the globe source or featured countries, use Node.js
20+ in a development checkout:

```text
npm ci
npm run build:globe
npm run test:globe
```

On Windows PowerShell, use `npm.cmd` if execution policy blocks `npm.ps1`.
The build uses Natural Earth 50m country boundaries through `world-atlas`,
exact per-pixel country IDs, and same-origin flags from `flag-icons`.
`@napi-rs/canvas` provides portable build-time rasterization instead of requiring
system Cairo libraries. Third-party notices ship alongside the generated assets.
Commit rebuilt assets when changing `frontend/globe.js`,
`frontend/globe-state.mjs`, or `scripts/build-region-textures.mjs`.

Run `venv\Scripts\python.exe app.py` to open the desktop application.
The shared sidebar links to four independently addressable pages:

| Page | URL | Purpose |
| --- | --- | --- |
| Overview | `/` | Live pipeline counts, recent prospects, recent hiring companies, and next actions |
| Prospects | `/prospects` | Search/filter prospects, inspect profiles, generate pitches, send outreach, and export CSV |
| Job Leads | `/job-leads` | Search/filter hiring companies, inspect job details, and update contact emails |
| Discovery | `/discovery` | Local Businesses and Hiring Companies search forms, map, and worker progress |
| Settings | `/settings` | Your sending mailbox, email signature, password, and signed-in devices |

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

### Website screenshots (private Cloudflare R2)

Screenshots are stored in a **private** Cloudflare R2 bucket and are skipped
entirely until R2 is configured. Create a bucket (keep public access off) and an
R2 API token with *Object Read & Write* on that bucket, then set:

| Variable | Value |
| --- | --- |
| `R2_ACCOUNT_ID` | Your Cloudflare account ID |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | The R2 API token's S3 credentials |
| `R2_BUCKET` | The bucket name |

The inspector loads `/api/prospects/<id>/screenshot`, which checks that the lead
belongs to the signed-in account and redirects to a 5-minute signed link.
`/static/screenshots/*` is no longer served.

Scraped websites are untrusted, so headless Chrome runs **with its sandbox**
(the app must run as a non-root user) and every request it makes, including
redirects, iframes and images, goes through a loopback filtering proxy
(`src/net_guard.py`) that only connects to public internet addresses.

Move screenshots captured before this change into R2 once (safe to re-run):

```powershell
python src\manage_screenshots.py upload-legacy --dry-run
python src\manage_screenshots.py upload-legacy
```

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
Facebook, Instagram, and LinkedIn company-page leads remain included regardless
of these filters, because these sources do not supply reliable star ratings and
review counts. LinkedIn company discovery searches public company pages indexed
by Google using Scrapingdog's Google Search API and `site:linkedin.com/company`
queries; it does not access LinkedIn accounts or private profiles.
Available discovery sources are Google Maps, Facebook, Instagram, LinkedIn
company pages, and Yelp. LinkedIn uses the location text directly and does not
require a map pin; selecting Google Maps still requires a validated pin. It
searches one Google results page (up to 10 organic results) per selected
industry, so the number saved may be lower after relevance checks and
deduplication. Scrapingdog's standard Google Search request costs 5 credits per
successful industry search.

New discoveries retain Google business website URLs, Instagram emails, and
Facebook/Instagram/LinkedIn company-page URLs. Social links are stored separately in
`profile_url`, exposed through the prospect queue and CSV export, and labeled
as social profiles in the prospect details; they are not treated as official
websites by scoring or website audits. Startup adds this nullable column
idempotently. This does not repair older discarded contact data or mislabeled
Facebook rows; those require separately approved recovery/backfill work.
Facebook/Instagram/LinkedIn discovery uses stable IDs based on canonical profile URLs,
and skips known or repeated profiles before Instagram contact crawling.
Tracking parameters, mobile/www hosts, casing, and trailing slashes do not
create new identities; Facebook numeric profile IDs remain distinct.
Older random-ID records with no saved profile URL cannot be matched reliably.

The map radius is a **strict Google Maps-only** distance filter for pinned
searches: results outside the circle or without valid result GPS coordinates
are logged and skipped before enrichment. The provider's map viewport is only a
search hint, not proof of distance. The API requires a valid pin and a 2-50 km
whole-number radius for Google Maps. Yelp, Facebook, Instagram, and LinkedIn use
location text rather than this circle; no extra per-business geocoding is performed.

Industry matching is conservative: Google Maps/Yelp require returned category
metadata matching a preset industry or its aliases, and save the matched
provider category rather than the search term. Facebook/Instagram require
matching title/snippet text; their stored category remains the target industry,
not an independently verified classification. All 32 presets have explicit
aliases; custom industries use normalized phrase matching. LinkedIn results are
also matched against title/snippet text. Missing evidence is logged and skipped.
This can exclude legitimate businesses with incomplete,
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
after a lost connection or app restart. Prospects supports individual pitch
drafting; the bulk drafting control is no longer available.
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
Each preset has four role queries; custom services have 1-4. Every role/page is
searched through both Scrapingdog Google Jobs and LinkedIn Jobs. The form and
trigger response display separate provider request counts and a job-search
credit estimate. Each request costs 5 Scrapingdog credits, so five pages for one
preset means up to 40 provider requests (20 per source, 200 credits), plus
separate Google Search requests for company-contact lookups. Contact lookups
also use 5 credits per request and are not included in the job-search estimate.
A page contains up to 10 raw postings before filtering/deduplication; more pages
do not guarantee more matching leads. Retries on transient provider errors may
add requests beyond the displayed estimate.

The trigger accepts integer `pages_per_role` (default 1) and returns the accepted
depth along with its run ID. Provider request and cost estimates are not
returned to the browser.
Invalid/unbounded depths are rejected before starting. Google Jobs pagination
follows `serpapi_pagination.next_page_token` at the fixed Scrapingdog endpoint,
keeping country/role parameters and resetting tokens for each role. LinkedIn
Jobs uses its page-number parameter at the `/jobs` endpoint. Both stop at the
page budget; Google also stops when no token remains. Repeated or malformed
tokens and later-page failures produce explicit partial-run errors; remaining
roles/services continue and valid postings can still be saved. Deduplication
covers both providers and all fetched pages before contact enrichment. Progress
logs identify the role, provider, and page; the run progress counter continues
to count services, not pages. No database-schema change is required for search
depth.

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
node --test tests\discovery-ui.test.js tests\workspace.test.js tests\account-plan.test.js tests\team.test.js tests\billing-ui.test.js
venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
```

Route tests replace environment loading and database access with mocks. Browser
QA should likewise use an isolated preview: importing the production API module
normally runs startup schema migrations.

Authenticated suites sign in through `tests/support.py`, which writes the
session directly so no users table read is needed. `tests/test_auth_isolation.py`
covers password hashing, non-enumerable sign-in failures, the absence of an
account lockout, open-redirect rejection (including backslash tricks), the
anonymous 401-JSON/302-page split, per-account query scoping, and
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

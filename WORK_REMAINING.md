# Work remaining: system review remediation

> **Companion document:** [WORK_DONE.md](./WORK_DONE.md) records everything finished so far.
> **Master plan:** [SYSTEM_REVIEW_PLAN.md](./SYSTEM_REVIEW_PLAN.md) holds each item's finding codes (NV-, CP-, PR-, FL-, UI-, AX-) and full context. Mark items ✅ there as they're finished, and move a summary to [WORK_DONE.md](./WORK_DONE.md).

Last updated: 2026-10-27. **Next item: Milestone A2.**

## How to resume

1. Read this file, then the matching section of [SYSTEM_REVIEW_PLAN.md](./SYSTEM_REVIEW_PLAN.md).
2. Work through the items in order, one at a time. For each item:
   - Present a plan with clarifying questions and get approval.
   - Implement it and add tests.
   - Run the full suites and do a live check.
   - Update the README and mark the item ✅ in the master plan and in this file.
3. Commit only when the user asks.

Suggested prompt when resuming: *"Continue the system review fixes from WORK_REMAINING.md, starting with the next item."*

## Milestone A: Trust, legal & broken paths (P0)

- [ ] **A2** Add Terms, Privacy, Refund Policy and Contact/Support pages, linked from every footer, Billing and Register (NV-02). The Privacy page should mention the hashed sign-up network used for trials (T9).
- [ ] **A3** Remove provider names, request counts and provider costs from the customer UI; describe cost in site credits only (CP-01).
- [ ] **A4** Mark unreleased features as "Coming soon" in the pricing table, or remove them (CP-02).
- [ ] **A5b** Fix the invitation hand-off: keep the token through sign-in and registration, disable Accept when there's no token, and use the public layout when signed out (FL-07, FL-08, FL-09).

## Milestone B: Conversion & navigation (P1)

- [ ] **B5 / B10a** Preserve purchase intent: anonymous plan CTAs go to `/register?next=/billing?plan=...`, `next` is carried between login and register, and the plan is pre-selected and highlighted on Billing (NV-03, PR-04, FL-06, FL-17).
- [ ] **B6** Styled upgrade/permission pages for 403s on HTML routes; upgrade links go to `/billing` (NV-04, NV-05).
- [ ] **B7 / B10b** Show live search cost next to the discovery submit button and limit pages-per-role by plan. Show plan limits in the form (industry counter, disabled extra boxes), use no default industry, resolve the location before the confirm step, and replace `confirm()` with an in-page cost summary (PR-02, PR-03, FL-10–FL-12).
- [ ] **B8** Generate the public pricing table from `plans.py` / `payments.PRICES` (PR-01).
- [ ] **B9** Plan-aware copy: "your credits" for Trial/Solo, "shared credits" for teams; fix "1 industries" (CP-06, CP-07).
- [ ] **B10** Home copy for 5 sources, LinkedIn Jobs and the free-trial CTA (CP-05). The trial now starts after email verification.
- [ ] **B10c** Clear outcome messages ("no credits charged"), hide internal log phrasing, and show visible errors for every failed action (FL-13, FL-15).
- [x] B10d: done in S1. B10e: done in S9/T8.

## Milestone C: Visual system & accessibility (P1–P2)

- [ ] **C11** Type scale; minimum 12px text, 13–14px buttons and nav (UI-01, UI-04).
- [ ] **C12** Darken light-theme `--subtle` (about `#6b7a83`) and `--muted` (about `#5a6c76`) for AA contrast (UI-02).
- [ ] **C13** Replace the runtime Tailwind browser build with component classes or a build-time compile (UI-03).
- [ ] **C14** Fonts: already self-hosted in T1; still reduce to 3 weights per family and preload (UI-05).
- [ ] **C15** One accessible modal helper (dialog role, Escape, focus trap and return) (AX-01).
- [ ] **C16** Clear locked-state style for plan-locked features (AX-02).
- [ ] **C17** Label all inputs, name pagination buttons, and move inline `onclick` handlers to listeners (AX-03, AX-04). Once this is done, the T1 report-only CSP can be enforced.
- [ ] **C18** Focus outlines on inputs; search and select-all in the industry picker (AX-05, AX-06).

## Milestone D: Copy polish & consistency (P2–P3)

- [ ] **D19** Rewrite jargon and spec-style text; collapsible "How this works" sections (CP-03, CP-04, CP-08).
- [ ] **D20** SVG icons instead of emoji; clarify "Approve & Send Test" (CP-09, CP-10).
- [ ] **D21** `Intl` formatting for credits and dates, `…`, consistent capitalization (UI-07, UI-08, CP-11).
- [ ] **D22** Simpler header and an account menu (NV-06–NV-09).
- [ ] **D23** `theme-color` meta, select styling, no `autofocus` on mobile (UI-06, AX-07).
- [ ] **D24** Field-level registration errors with `aria-invalid`; password strength and common-password check (FL-04, FL-05).
- [ ] **D25** Disable Export with no leads, one date format everywhere, focus into the mobile drawer (FL-16, FL-18, FL-19).

## Verification for every milestone

See "Verification for every milestone" in [SYSTEM_REVIEW_PLAN.md](./SYSTEM_REVIEW_PLAN.md):

- Full suites.
- Contrast and text-size checks at 390 and 1440 px, in light and dark themes.
- A keyboard-only pass through register → discovery → billing.
- The security regression list.

## Deployment follow-ups for the owner (not code)

- [ ] Rotate `FLASK_SECRET_KEY` (signs everyone out once).
- [ ] Create the R2 bucket, set the `R2_*` variables, and run `python -B src/manage_screenshots.py upload-legacy`.
- [ ] Purge `static/screenshots` from git history after committing.
- [ ] In production set:
  - `APP_ENV=production`
  - `TRUSTED_PROXY_HOPS=1` (behind Render, Railway, Heroku or Nginx)
  - `APP_BASE_URL=https://...`
  - `TRIAL_IP_HASH_SECRET` (new in T9; keep it stable)
  - `SYSTEM_SMTP_PASSWORD` and `SYSTEM_SMTP_FROM` (Resend; production refuses to start without them)
- [ ] Start production with gunicorn using `gunicorn.conf.py`.
- [ ] Set PostgreSQL `log_min_error_statement=panic`; check `SHOW timezone`.
- [ ] Reinstall the pinned requirements: `pip install -r requirements-desktop.txt` (your global Python still has Werkzeug 3.1.8).
- [ ] Commit the work (nothing is committed yet). The `src/__pycache__/*.pyc` files show as modified and shouldn't be committed.

## Developer notes for resuming

- **Python tests:** run `$env:PYTHONIOENCODING='utf-8'; python -B -m unittest discover -s tests`. Running a module directly (`python -m unittest tests.x`) fails on the `support` import.
- **Database tests:** set `TEST_BILLING_DSN='host=127.0.0.1 port=55439 dbname=billing_test user=team_test'` before running the Python tests.
- **Temporary PostgreSQL** (binaries in `C:\Program Files\PostgreSQL\18\bin`):
  1. `initdb -U team_test -A trust -E UTF8 --no-locale`.
  2. Run `pg_ctl ... -o "-p 55439" -w start` in its own background shell and leave it running; check with `pg_isready`.
  3. Run `createdb billing_test` and the tests from another shell.
  4. Stop with `pg_ctl -m fast -w stop`.
- **JS tests:** `node --test "tests/*.test.js"`.
- **Preview server:**
  - Set `APP_ENV=local` and `DATABASE_URL=postgresql://team_test@127.0.0.1:55439/lead_review`, after loading `schema.sql` with `psql`.
  - Run with `load_dotenv` patched out.
  - In local mode, emails are printed to the console as `[DEV MAIL]`.
- **CSP:** no inline scripts in new code; put JS in `static/*.js`.
- **CSRF:** POSTs need `X-CSRF-Token` (from `meta[name="csrf-token"]`) or a hidden `csrf_token` form field.
- **Line endings:** `SYSTEM_REVIEW_PLAN.md` uses CRLF.

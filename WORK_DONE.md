# Work done: system review remediation

> **Companion document:** [WORK_REMAINING.md](./WORK_REMAINING.md) lists what is still to do and how to resume.
> **Master plan:** [SYSTEM_REVIEW_PLAN.md](./SYSTEM_REVIEW_PLAN.md) holds the full findings (Phases 1–3) and the implementation plan. Items marked ✅ there are done.

Last updated: 2026-11-02. Nothing has been committed yet; all changes are in the working tree.

## Status at a glance

| Milestone | Status |
| --- | --- |
| S: Security hotfix (S1–S11) | ✅ Done |
| T: Hardening (T1–T9) | ✅ Done |
| A: Trust, legal & broken paths | ✅ Done |
| B: Conversion & navigation | ✅ Done |
| C: Visual system & accessibility | ✅ Done (C11–C18) |
| D: Copy polish & consistency | ✅ Done (D19–D25) |

Latest full-suite validation: **372 Python tests (52 skipped) and 108 JavaScript tests pass.**

## How each fix was done

For every item: plan and clarifying questions first, then the change, tests (unit, full suites with a temporary PostgreSQL, and a live check on a preview server), a README update, and a ✅ in the master plan. The same approach continues in [WORK_REMAINING.md](./WORK_REMAINING.md).

## Milestone S: Security hotfix

| Item | What was done | Main files |
| --- | --- | --- |
| S1 XSS | Dynamic `innerHTML` replaced with elements and `textContent`; custom business types capped at 60 characters | `static/workspace.js`, `src/plans.py` |
| S2 Screenshots | Sandboxed Chrome behind a filtering proxy; screenshots go to a private Cloudflare R2 bucket and are served through an owner-checked, signed redirect; `/static/screenshots/*` returns 404 | `src/screenshot_agent.py`, `src/r2_storage.py`, `src/net_guard.py`, `src/manage_screenshots.py` |
| S3 SSRF (crawling) | `net_guard.safe_get`: IP re-checked at connect time, same-site redirects only, 2 MB cap, timeouts | `src/net_guard.py`, `src/email_hunter.py` |
| S4 SSRF (SMTP) | Only public mail hosts and ports 465/587/2525; connects to the checked IP; short user-facing errors | `src/user_email.py` |
| S5 AI pitches | Bulk pitch removed; 1 credit per draft with an automatic refund on failure; no provider errors shown | `src/ai_pitch.py` |
| S6 Background jobs | Per-user job guard, cooldowns, `GET /api/jobs/status` | `src/background_jobs.py` |
| S7 Pagination | Validated and clamped `page`/`per_page` | `src/control_gate_api.py` |
| S8 Error pages | Real status codes, JSON for `/api/*`, `error.html` for pages, generic 500 with a reference code | `src/templates/error.html` |
| S9 Rate limits | PostgreSQL fixed-window limits (hashed keys); account lockout removed | `src/rate_limit.py` |
| S10 CSV export | Formula-injection protection, status validation, safe file names | `src/control_gate_api.py` |
| S11 Lockout time zone | Resolved by S9 | n/a |

## Milestone T: Hardening

| Item | What was done | Main files |
| --- | --- | --- |
| T1 Security headers | Enforced CSP plus a strict report-only CSP; self-hosted vendor assets; HSTS outside local mode | `src/security_headers.py`, `static/vendor/`, `gunicorn.conf.py` |
| T2 CSRF | One app-wide check on every mutating request, plus an Origin/Referer check | `src/csrf.py` |
| T3 Sessions | `users.session_version`; change password; sign out of all devices | `src/auth.py`, `static/account_security.js` |
| T4 App environment | `APP_ENV` (`local`/`desktop`/`production`/`staging`); hosted envs require `FLASK_SECRET_KEY` | `src/app_env.py`, `app.py` |
| T5 Outreach email | Recipient validated, no fallback to the sender, HTML escaped, SMTP passwords used as typed | `src/user_email.py` |
| T6 Logs | Emails redacted in logs; provider cost and errors removed from responses | `src/log_redact.py` |
| T7 Dependencies | All direct dependencies pinned, pip-audit clean, separate desktop requirements, Python 3.12 | `requirements.txt`, `requirements-desktop.txt`, `.python-version`, `build.bat` |
| T8 Redirects and auth messages | `safe_redirect_target` rejects backslashes, `%5C`, whitespace, control characters, schemes and hosts | `src/auth.py` |
| T9 Email verification and trials | See below | see below |

### T9 details (latest work)

- **Sign-up:** the user is signed in immediately but unverified. A banner says "Verify your email to start your free trial" and has a Resend button.
- **Verification:** the link lasts 24 hours and works once. The token is in the URL fragment (`/verify-email#token`). The 7-day, 30-credit trial starts when the email is verified.
- **One trial per network:** IPv4 addresses are matched exactly and IPv6 addresses by /64. Only an HMAC keyed by `TRIAL_IP_HASH_SECRET` is stored, and the claim is permanent. Local and desktop runs skip the rule. Invited members are exempt. Support can override with `python src/manage_plans.py activate <email> trial`.
- **Forgot password:** sends a reset link that lasts 1 hour and works once; requesting another cancels the earlier one. The reply is the same whether or not the email exists, and the email is sent in the background so timing doesn't reveal it.
- **Reset:** raises `session_version`, which signs out every device, then redirects to `/login?reset=done`. A reset also counts as verifying the email.
- **Existing users** were backfilled as verified by the migration.
- **Hosted environments refuse to start** without `TRIAL_IP_HASH_SECRET` and system mail (`SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM` and an https `APP_BASE_URL`). Local and desktop runs print emails to the console between `[DEV MAIL]` markers.
- **New files:** `src/account_email.py`, `src/account_api.py`, `src/system_mail.py`, `src/templates/verify_email.html`, `static/account_email.js`, `tests/test_account_email.py`.
- **Changed files:** `src/auth.py`, `src/billing.py` (`grant_trial`, `trial_network_free`), `src/payments.py`, `src/teams.py`, `src/csrf.py`, `src/rate_limit.py`, `src/control_gate_api.py`, `src/templates/auth.html`, `base.html`, `billing.html`, `plans.html`, `static/dashboard.css`, `README.md`.

## Earlier feature work (before the review)

- Discovery fixes, including LinkedIn Jobs hiring discovery.
- Plans: Trial, Solo, Small Team and Enterprise, with source and feature locks and upgrade prompts.
- Teams: User Management, shared owner credits, seats and email invitations.
- PayMongo QR Ph billing and top-ups, and the public plans page.
- Live credits card in the sidebar.
- Bulk draft pitch removed from Prospects.

## Milestone items already covered by S and T

- **A1** (error pages): done in S8.
- **A5a** (`locked_until` time zone): done in S9/S11.
- **A5c** (password reset, change password, email verification): done in T3 and T9.
- **A2** (public policies and support): added `/terms`, `/privacy`, `/refund-policy`, and `/support`; linked them from public and workspace footers, Billing, and registration. The Privacy Policy explains the keyed trial-network hash. Because the operator details and support address were not provided, policy copy avoids inventing them and the Support page says no direct email is configured.
- **A3** (provider internals in customer copy): removed the search vendor name, upstream request counts, and vendor-credit costs from discovery and pricing UI. The hiring estimate now reports only site search credits based on selected roles and page depth.
- **A4** (unreleased plan features): removed Ad Pixel Check, bulk CSV enrichment, and webhook/API export from the public plan comparison and removed the stale planned-features disclaimer.
- **A5b** (team invitation hand-off): keep the email-link token in tab-scoped storage through sign-in/registration; preserve `next` between auth pages; disable acceptance until a token is available; show the public layout while signed out; and give an actionable resend message for expired/invalid invitations.
- **B5/B10a** (purchase intent): anonymous plan CTAs carry the selected plan and anchor through registration, sign-in/register links preserve `next`, and Billing validates/preselects/highlights the requested plan. Added a Free Trial CTA targeting the Billing trial offer.
- **B6** (plan permissions): browser-side 403 plan/permission denials render the styled error page with Billing/workspace links; API denials stay JSON; discovery upgrade prompts link to `/billing`.
- **B7/B10b** (discovery cost and plan limits): use server-provided prices for live estimates; display and enforce industry/depth limits in the form, default to no selected industry, resolve local location before quoting, and confirm the server quote in-page instead of `confirm()`.
- **B8** (pricing drift): generate public plan cards, comparison rows, and pricing metadata from `plans.py` and `payments.PRICES`; use canonical plan IDs and server-owned charge/refund rates in the pricing explanation. A source-mutation regression verifies that published values change with the underlying plan and checkout data.
- **B9** (plan-aware credit copy): Trial/Solo now refer to the user's balance, while Small Team/Enterprise identify the shared balance in discovery, sidebar, Billing, and top-up copy. Singular industry wording is used for one-industry limits; tests cover both account types.
- **B10** (home marketing copy): name Google Maps, Yelp, Facebook, Instagram, and LinkedIn company pages; identify Google Jobs and LinkedIn Jobs; add a plans link and a free-trial CTA to registration, with trial timing tied to email verification.
- **B10c** (discovery outcomes and action errors): explicitly say when zero leads were found and no credits were charged; show only customer-facing progress instead of worker/provider logs; hide raw provider errors in failure notices; make email-update, archive, and send failures visible with retry guidance.
- **C11** (typography): added a shared 12/13/14/16/18/22/28/36px scale, raised smaller text to 12px, and set navigation and action controls to 14px. The dashboard page suite and all 92 JavaScript tests pass.
- **C12** (light-theme contrast): changed `--subtle` to `#626f78` and `--muted` to `#5a6c76`. A regression test checks both colors against all four light surfaces; the lowest ratio is 4.58:1.
- **C13** (Tailwind runtime): compile template utilities into `static/tailwind.css` at build time, with theme-aware color overrides; remove the browser runtime asset and attribute-selector remapping. The compiled CSS is 34.6 KB.
- **C14** (fonts): standardize DM Sans on 400/600/700 and Manrope on 600/700/800, map Tailwind's medium utility to semibold, replace the CSS import with explicit stylesheet links, and preload the self-hosted Latin subsets while retaining Latin-ext fallback. Dashboard tests verify all font assets, preload tags, and the reduced weight set.
- **C15** (accessible dialogs): add a shared dialog helper that sets modal semantics, moves focus into the dialog, traps Tab navigation, handles Escape according to dialog behavior, and restores focus on close. Apply it to prospect/job profiles, screenshot lightbox, and discovery progress; keep the dedicated discovery form as a labeled region. Add accessible names for icon-only close buttons and tests for focus handling and dialog markup. All 95 JavaScript tests and 31 dashboard tests pass.
- **C16** (plan-locked styling): visually distinguish disabled discovery sources, hiring controls, and custom-service editing; show compact, emphasized upgrade links; and mute the disabled hiring-submit button. Preserve server and client entitlement checks. Plan-capability tests verify locked-state classes, prompt labels, and submit behavior.
- **C17** (accessible controls and listeners): label all rendered workspace form controls, give pagination buttons descriptive names, and replace inline event-handler attributes with delegated JavaScript listeners. Convert dynamic table-row/page and screenshot-error handlers to `addEventListener`. Rendered-page tests cover discovery, prospects, jobs, and settings; template parsing confirms there are no inline event-handler attributes. The inline dashboard script and styles remain, so CSP enforcement is still a separate follow-up.
- **C18** (focus visibility and industry picker): restore strong focus-visible outlines for auth, settings, and workspace form controls; make industries searchable and add a select-visible action that continues to honor plan caps while retaining selections hidden by the search. Focused discovery/account-plan and rendered-page tests pass.
- **D19** (plain-language copy): replace internal workspace jargon, clarify purchase availability without exposing setup terminology, and move detailed discovery matching, location, and pagination explanations into expandable help. Dashboard, billing, and workspace interaction regressions pass.
- **D20** (icons and recipient clarity): replace dashboard emoji and bare close characters with the shared SVG icon macro; label the destination as a real recipient address, defaulted from the prospect record, and rename the stale test-email input/button copy. Rendered-page and interaction regressions cover the revised markup and dispatch field.
- **D21** (formatting and terminology): use the same locale-aware credit and date formats across account status, the sidebar, Billing and search quotes; replace user-facing dashboard triple dots with `…`; standardize "Best Value" and website contact feature names. Focused UI and rendered-page tests cover the formats and copy.
- **D22** (workspace navigation): remove repeated discovery calls to action and top-bar branding/credit status, group Billing and User Management under More, and add a keyboard-operable account menu with email, settings, password change, and CSRF-protected sign-out. Add Home and Plans & Pricing links to the shared public footer. Full Python (368 tests; 52 skipped) and JavaScript (102 tests) suites pass.
- **D23** (theme and focus polish): add a browser theme-color meta tag to every page template and update it with the selected light/dark theme; apply consistent, theme-aware styling to native selects; replace native auth autofocus with desktop/fine-pointer-only focus to avoid opening mobile keyboards. Full Python (369 tests; 52 skipped) and JavaScript (103 tests) suites pass.
- **D24** (registration feedback and password strength): associate server validation errors with the affected fields, set `aria-invalid`, preserve non-password values, and focus the first invalid field on fine-pointer devices. Add a live strength meter and server-side minimum-strength/common-password checks (including password reset). This uses a small known-common-password blocklist, not a breached-password lookup. Full Python (371 tests; 52 skipped) and JavaScript (105 tests) suites pass.
- **D25** (CSV export, dates, and mobile navigation): keep CSV export disabled until the account has prospects and hide it for Job Leads; verify consistent locale-aware Billing/sidebar date formatting; use an action-oriented top-up label; move focus into the mobile navigation drawer and return it to the menu button on close. Full Python (372 tests; 52 skipped) and JavaScript (108 tests) suites pass.
- **B10d** (escaping `innerHTML`): done in S1.
- **B10e** (generic auth messages, rate limits): done in S9 and T8.

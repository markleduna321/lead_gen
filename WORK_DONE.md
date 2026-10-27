# Work done: system review remediation

> **Companion document:** [WORK_REMAINING.md](./WORK_REMAINING.md) lists what is still to do and how to resume.
> **Master plan:** [SYSTEM_REVIEW_PLAN.md](./SYSTEM_REVIEW_PLAN.md) holds the full findings (Phases 1–3) and the implementation plan. Items marked ✅ there are done.

Last updated: 2026-10-27. Nothing has been committed yet; all changes are in the working tree.

## Status at a glance

| Milestone | Status |
| --- | --- |
| S: Security hotfix (S1–S11) | ✅ Done |
| T: Hardening (T1–T9) | ✅ Done |
| A: Trust, legal & broken paths | Partly done (A1, A5a, A5c) |
| B: Conversion & navigation | Partly done (10d, 10e) |
| C: Visual system & accessibility | Not started |
| D: Copy polish & consistency | Not started |

Latest test run: **350 Python tests (including the PostgreSQL integration tests) and 81 JS tests pass.**

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
- **B10d** (escaping `innerHTML`): done in S1.
- **B10e** (generic auth messages, rate limits): done in S9 and T8.

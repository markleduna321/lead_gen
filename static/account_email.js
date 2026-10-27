// Email verification, password reset links and the "verify your email" banner.
// Tokens arrive in the URL fragment so they never reach server or proxy logs.
(function () {
    "use strict";

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    }

    function takeFragmentToken() {
        const token = window.location.hash.replace(/^#/, "");
        if (token) {
            window.history.replaceState(null, "", window.location.pathname + window.location.search);
        }
        return token;
    }

    async function postJson(url, body) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
            body: JSON.stringify(body || {}),
        });
        let data = {};
        try { data = await response.json(); } catch (error) { data = {}; }
        return { ok: response.ok, data };
    }

    async function verifyPage() {
        const status = document.getElementById("verify-status");
        const failure = document.getElementById("verify-error");
        const next = document.getElementById("verify-continue");
        const token = takeFragmentToken();
        const fail = (message) => {
            status.textContent = "We couldn't verify your email.";
            failure.textContent = message;
            failure.hidden = false;
        };
        if (!token) {
            fail("This link is incomplete. Open the link from your email, or sign in and choose Resend email.");
            return;
        }
        try {
            const { ok, data } = await postJson("/api/auth/verify-email", { token });
            if (!ok) {
                fail(data.message || "This link is invalid or has expired. Sign in and choose Resend email.");
                return;
            }
            status.textContent = data.message || "Your email is verified.";
            next.hidden = false;
        } catch (error) {
            fail("We couldn't reach the server. Check your connection and reload this page.");
        }
    }

    function resetPage() {
        const input = document.getElementById("reset-token");
        if (!input) return;
        const token = takeFragmentToken();
        if (token) input.value = token;
        if (!input.value) {
            document.getElementById("auth-link-error").hidden = false;
        }
    }

    function banner() {
        const button = document.getElementById("resend-verification");
        const status = document.getElementById("resend-verification-status");
        if (!button || !status) return;
        button.addEventListener("click", async () => {
            button.disabled = true;
            status.textContent = "Sending...";
            try {
                const { data } = await postJson("/api/auth/resend-verification");
                status.textContent = data.message || "We couldn't send the email right now. Try again later.";
                if (data.verified) window.location.reload();
            } catch (error) {
                status.textContent = "We couldn't reach the server. Check your connection and try again.";
            } finally {
                button.disabled = false;
            }
        });
    }

    function start() {
        if (document.body.dataset.page === "verify-email") verifyPage();
        else if (document.body.dataset.authMode === "reset") resetPage();
        banner();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();

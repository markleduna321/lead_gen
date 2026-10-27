// Settings -> Account security: change password and sign out of all devices.
(() => {
    const form = document.getElementById('password-change-form');
    const everywhere = document.getElementById('btn-sign-out-everywhere');
    if (!form || !everywhere) return;
    const errorBox = document.getElementById('account-security-error');
    const noticeBox = document.getElementById('account-security-notice');
    const show = (box, text) => {
        errorBox.classList.add('hidden');
        noticeBox.classList.add('hidden');
        if (!box) return;
        box.textContent = text;
        box.classList.remove('hidden');
    };
    const field = id => document.getElementById(id);
    let working = false;

    async function post(url, body) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body || {})
        });
        let data = {};
        try { data = await response.json(); } catch (error) { data = {}; }
        if (!response.ok || data.status !== 'success') {
            throw new Error(data.message || 'That did not work. Please try again.');
        }
        return data;
    }

    async function busy(buttons, work) {
        if (working) return;
        working = true;
        buttons.forEach(button => { button.disabled = true; });
        try {
            await work();
        } catch (error) {
            show(errorBox, error.message);
        } finally {
            working = false;
            buttons.forEach(button => { button.disabled = false; });
        }
    }

    form.addEventListener('submit', event => {
        event.preventDefault();
        const current = field('current-password');
        const next = field('new-password');
        const confirmation = field('confirm-new-password');
        if (!current.value) { show(errorBox, 'Enter your current password.'); current.focus(); return; }
        if (next.value.length < 10) { show(errorBox, 'Use a new password with at least 10 characters.'); next.focus(); return; }
        if (next.value !== confirmation.value) { show(errorBox, 'Both new passwords must match.'); confirmation.focus(); return; }
        busy([field('btn-change-password')], async () => {
            const data = await post('/api/account/password', {
                current_password: current.value,
                new_password: next.value,
                confirm_password: confirmation.value
            });
            form.reset();
            show(noticeBox, data.message);
        });
    });

    everywhere.addEventListener('click', () => {
        if (!confirm('Sign out of all devices? You will need to sign in again here too.')) return;
        busy([everywhere], async () => {
            const data = await post('/api/account/sign-out-everywhere');
            window.location.assign(data.redirect || '/login');
        });
    });
})();

(() => {
    const notice = document.getElementById('team-message');
    let working = false;
    const message = (text, error = false) => {
        notice.textContent = text;
        notice.className = error ? 'settings-error' : 'settings-notice';
        notice.hidden = false;
    };
    async function mutate(url, method, body) {
        const response = await fetch(url, {
            method,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
            },
            body: body === undefined ? undefined : JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'The operation failed.');
        return data;
    }
    async function action(work) {
        if (working) return;
        working = true;
        document.querySelectorAll('button, input').forEach(element => {
            element.dataset.wasDisabled = String(element.disabled);
            element.disabled = true;
        });
        try {
            await work();
        } catch (error) {
            message(`${error.message} Reload to check the outcome before retrying.`, true);
        } finally {
            working = false;
            document.querySelectorAll('button, input').forEach(element => {
                element.disabled = element.dataset.wasDisabled === 'true';
            });
        }
    }
    const form = document.getElementById('team-invite-form');
    if (form) form.addEventListener('submit', event => {
        event.preventDefault();
        const email = document.getElementById('team-invite-email').value.trim();
        action(async () => {
            await mutate('/api/team/invitations', 'POST', {email});
            window.location.reload();
        });
    });
    document.querySelectorAll('[data-remove-member], [data-revoke-invite]').forEach(button => {
        button.addEventListener('click', () => {
            if (!confirm('Remove this member or revoke this invitation? This releases the seat.')) return;
            const url = button.dataset.removeMember
                ? `/api/team/members/${button.dataset.removeMember}`
                : `/api/team/invitations/${button.dataset.revokeInvite}`;
            action(async () => { await mutate(url, 'DELETE'); window.location.reload(); });
        });
    });
    if (document.body.dataset.page === 'team-invitation') {
        let token;
        try {
            if (window.location.hash) {
                sessionStorage.setItem('team-invitation', window.location.hash.slice(1));
                history.replaceState(null, '', window.location.pathname);
            }
            token = sessionStorage.getItem('team-invitation');
        } catch (error) {
            message('Your browser could not retain the invitation. Allow session storage and reopen the email link.', true);
        }
        const accept = document.getElementById('accept-team-invitation');
        if (accept) {
            accept.disabled = !token;
            if (!token) message('Open the invitation link from your email to continue.', true);
            accept.addEventListener('click', () => action(async () => {
                await mutate('/api/team/accept', 'POST', {token});
                sessionStorage.removeItem('team-invitation');
                message('You joined the team. Discovery now uses the shared owner balance; your leads remain private.');
                accept.hidden = true;
                await window.refreshAccountPlan?.();
            }));
        }
    }
})();

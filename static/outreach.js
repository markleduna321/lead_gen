(() => {
    const modal = document.getElementById('outreach-modal');
    if (!modal) return;
    const form = document.getElementById('outreach-form');
    const select = document.getElementById('outreach-next-status');
    const note = document.getElementById('outreach-note');
    const save = document.getElementById('outreach-save');
    const message = document.getElementById('outreach-message');
    const history = document.getElementById('outreach-history');
    const close = document.getElementById('outreach-close');
    const actions = document.querySelectorAll('[data-outreach-status]');
    let target = null;
    let requestId = 0;
    let saving = false;
    function render(data) {
        target.status = data.outreach_status;
        select.value = data.outreach_status;
        history.replaceChildren();
        data.history.forEach(event => {
            const item = document.createElement('li');
            const heading = document.createElement('strong');
            heading.textContent = `${outreachLabel(event.previous_status)} → ${outreachLabel(event.status)}`;
            const time = document.createElement('small');
            time.textContent = `${new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'})
                .format(new Date(event.created_at))}${event.source === 'email_sent' ? ' · Sent in app' : ' · Manual update'}`;
            const text = document.createElement('p');
            text.textContent = event.note;
            item.append(heading, time, text);
            history.appendChild(item);
        });
        if (!data.history.length) {
            const item = document.createElement('li');
            item.textContent = 'No recorded updates yet. Existing sent leads start as awaiting reply.';
            history.appendChild(item);
        }
    }
    function setBusy(busy) {
        save.disabled = busy;
        select.disabled = busy;
        note.disabled = busy;
        actions.forEach(button => { button.disabled = busy; });
    }
    window.openOutreachTracker = async (kind, lead) => {
        if (saving) return;
        const id = ++requestId;
        target = {url: `/api/outreach/${kind}/${lead.id}`, status: null,
            returnFocusId: `outreach-${kind}-${lead.id}`};
        document.getElementById('outreach-lead-name').textContent = lead.business_name || lead.company_name;
        note.value = '';
        history.replaceChildren();
        message.textContent = 'Loading outreach history…';
        setBusy(true);
        openAccessibleModal(modal, {onEscape: () => close.click()});
        try {
            const response = await fetch(target.url, {cache: 'no-store'});
            const data = await response.json();
            if (id !== requestId) return;
            if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Outreach could not be loaded.');
            render(data);
            message.textContent = '';
            setBusy(false);
        } catch (error) {
            if (id === requestId) message.textContent = `${error.message} Close and reopen tracking to retry.`;
        }
    };
    close.addEventListener('click', () => {
        if (saving) return;
        requestId++;
        closeAccessibleModal(modal);
        const returnFocus = document.getElementById(target.returnFocusId) || document.getElementById('filter-outreach');
        returnFocus?.focus();
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (saving || save.disabled || !target?.status) return;
        saving = true;
        let retrySafe = false;
        openAccessibleModal(modal, {closeOnEscape: false});
        setBusy(true);
        close.disabled = true;
        message.textContent = 'Saving update…';
        try {
            const response = await fetch(target.url, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({status: select.value, expected_status: target.status, note: note.value})
            });
            const data = await response.json();
            retrySafe = response.status < 500;
            if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Update could not be saved.');
            render(data);
            note.value = '';
            message.textContent = 'Outreach update saved.';
            try {
                await loadQueue();
            } catch (error) {
                message.textContent = `Outreach update saved, but the table could not refresh: ${error.message}`;
            }
        } catch (error) {
            message.textContent = retrySafe
                ? `${error.message} Your note is kept.`
                : `${error.message} The save could not be confirmed. Your note is kept here; copy it before closing and reopening tracking to check whether it saved.`;
        } finally {
            saving = false;
            close.disabled = false;
            setBusy(!retrySafe);
            openAccessibleModal(modal, {onEscape: () => close.click()});
        }
    });
    actions.forEach(button => {
        button.addEventListener('click', () => {
            if (save.disabled) return;
            select.value = button.dataset.outreachStatus;
            form.requestSubmit();
        });
    });
})();

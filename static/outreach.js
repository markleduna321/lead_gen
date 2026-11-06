(() => {
    const modal = document.getElementById('outreach-modal');
    if (!modal) return;
    const $ = id => document.getElementById(id);
    const form = $('outreach-form');
    const select = $('outreach-next-status');
    const note = $('outreach-note');
    const save = $('outreach-save');
    const message = $('outreach-message');
    const history = $('outreach-history');
    const close = $('outreach-close');
    const actions = document.querySelectorAll('[data-outreach-status]');
    const tabs = [$('outreach-tab-status'), $('outreach-tab-call')];
    const callForm = $('outreach-call-form');
    const callSave = $('outreach-call-save');
    const callNote = $('outreach-call-note');
    const callPhone = $('outreach-call-phone');
    const callTime = $('outreach-call-time');
    const callbackAt = $('outreach-callback-at');
    const callbackRow = $('outreach-callback-row');
    const outcomes = $('outreach-call-outcomes');
    const phoneDisplay = $('outreach-phone-display');
    const phoneSource = $('outreach-phone-source');
    const callLink = $('outreach-call-link');
    const copyPhone = $('outreach-copy-phone');
    const outcomeLabels = {
        no_answer: 'No answer', voicemail: 'Left voicemail', connected: 'Spoke with them',
        callback_requested: 'Callback requested', interested: 'Interested / meeting booked',
        not_interested: 'Not interested', wrong_number: 'Wrong or invalid number'
    };
    const sourceLabels = {manual: 'Entered manually', listed: 'Listed', listed_unverified: 'Listed, unverified'};
    let target = null;
    let requestId = 0;
    let saving = false;
    const formatTime = value => new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'})
        .format(new Date(value));
    const localInputValue = date => {
        const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
        return local.toISOString().slice(0, 16);
    };
    const outcomeInputs = () => outcomes.querySelectorAll('input[name="outreach-call-outcome"]');
    const selectedOutcome = () => [...outcomeInputs()].find(input => input.checked)?.value || '';

    function renderPhone(calls) {
        const phone = calls?.phone || '';
        target.phone = phone;
        phoneDisplay.textContent = phone || 'No number saved';
        const parts = [];
        if (phone) parts.push(sourceLabels[calls.phone_source] || 'Listed');
        if (calls?.phone_invalid) parts.push('Flagged as wrong or invalid');
        if (calls?.call_attempts) parts.push(`${calls.call_attempts} call${calls.call_attempts === 1 ? '' : 's'} logged`);
        if (calls?.callback_at) parts.push(`Callback ${formatTime(calls.callback_at)}`);
        phoneSource.textContent = parts.join(' · ');
        phoneDisplay.parentElement.classList.toggle('outreach-phone-invalid', Boolean(calls?.phone_invalid));
        callLink.classList.toggle('hidden', !phone);
        copyPhone.classList.toggle('hidden', !phone);
        callLink.href = phone ? `tel:${phone.replace(/[^\d+]/g, '')}` : '#';
        callLink.setAttribute('aria-label', phone ? `Call ${phone} on this device` : 'Call');
        callPhone.value = phone;
        callPhone.placeholder = phone ? '' : 'Enter the number you dialed';
    }

    function render(data) {
        target.status = data.outreach_status;
        select.value = data.outreach_status;
        renderPhone(data.calls);
        history.replaceChildren();
        data.history.forEach(event => {
            const item = document.createElement('li');
            const heading = document.createElement('strong');
            const change = event.previous_status === event.status
                ? `Status unchanged (${outreachLabel(event.status)})`
                : `${outreachLabel(event.previous_status)} → ${outreachLabel(event.status)}`;
            heading.textContent = event.channel === 'call'
                ? `Call · ${outcomeLabels[event.call_outcome] || 'Logged'} · ${change}`
                : change;
            const time = document.createElement('small');
            const details = [formatTime(event.occurred_at || event.created_at)];
            if (event.channel === 'call') {
                if (event.phone) details.push(event.phone);
                if (event.callback_at) details.push(`Callback ${formatTime(event.callback_at)}`);
            } else details.push(event.source === 'email_sent' ? 'Sent in app' : 'Manual update');
            time.textContent = details.join(' · ');
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
        [save, select, note, callSave, callNote, callPhone, callTime, callbackAt, copyPhone]
            .forEach(control => { control.disabled = busy; });
        outcomeInputs().forEach(input => { input.disabled = busy; });
        actions.forEach(button => { button.disabled = busy; });
    }

    function selectTab(tab, focus = false) {
        tabs.forEach(item => {
            const active = item === tab;
            item.setAttribute('aria-selected', String(active));
            item.tabIndex = active ? 0 : -1;
            $(item.getAttribute('aria-controls')).classList.toggle('hidden', !active);
        });
        if (focus) tab.focus();
    }

    function resetCallForm() {
        outcomeInputs().forEach(input => { input.checked = false; });
        callNote.value = '';
        callbackAt.value = '';
        callbackRow.classList.add('hidden');
        callTime.value = localInputValue(new Date());
    }

    window.openOutreachTracker = async (kind, lead, options = {}) => {
        if (saving) return;
        const id = ++requestId;
        target = {url: `/api/outreach/${kind}/${lead.id}`, status: null, phone: '',
            returnFocusId: `outreach-${kind}-${lead.id}`};
        $('outreach-lead-name').textContent = lead.business_name || lead.company_name;
        note.value = '';
        resetCallForm();
        renderPhone(null);
        selectTab(options.tab === 'call' ? tabs[1] : tabs[0]);
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
        const returnFocus = (target && document.getElementById(target.returnFocusId)) || $('filter-outreach');
        returnFocus?.focus();
    });

    // Retry is only offered when the server definitely rejected the request;
    // otherwise a retry could record the same update twice.
    async function submit(url, payload, pending, done, keptText, onSaved) {
        saving = true;
        let retrySafe = false;
        openAccessibleModal(modal, {closeOnEscape: false});
        setBusy(true);
        close.disabled = true;
        message.textContent = pending;
        try {
            const response = await fetch(url, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
            });
            const data = await response.json();
            retrySafe = response.status < 500;
            if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Update could not be saved.');
            render(data);
            onSaved();
            message.textContent = done;
            try {
                await loadQueue();
            } catch (error) {
                message.textContent = `${done.replace(/\.$/, '')}, but the table could not refresh: ${error.message}`;
            }
        } catch (error) {
            message.textContent = retrySafe
                ? `${error.message} ${keptText}`
                : `${error.message} The save could not be confirmed. Your entry is kept here; copy any note before closing and reopening tracking to check whether it saved.`;
        } finally {
            saving = false;
            close.disabled = false;
            setBusy(!retrySafe);
            openAccessibleModal(modal, {onEscape: () => close.click()});
        }
    }

    form.addEventListener('submit', event => {
        event.preventDefault();
        if (saving || save.disabled || !target?.status) return;
        return submit(target.url, {status: select.value, expected_status: target.status, note: note.value},
            'Saving update…', 'Outreach update saved.', 'Your note is kept.', () => { note.value = ''; });
    });

    callForm.addEventListener('submit', event => {
        event.preventDefault();
        if (saving || callSave.disabled || !target?.status) return;
        const outcome = selectedOutcome();
        if (!outcome) {
            message.textContent = 'Choose how the call went.';
            outcomeInputs()[0]?.focus();
            return;
        }
        if (!callTime.value) {
            message.textContent = 'Enter when you made the call.';
            callTime.focus();
            return;
        }
        const payload = {outcome, expected_status: target.status, note: callNote.value,
            occurred_at: new Date(callTime.value).toISOString()};
        const phone = callPhone.value.trim();
        if (phone) payload.phone = phone;
        if (outcome === 'callback_requested' && callbackAt.value) {
            payload.callback_at = new Date(callbackAt.value).toISOString();
        }
        return submit(`${target.url}/calls`, payload, 'Saving call…', 'Call logged.', 'Your call details are kept.',
            resetCallForm);
    });

    outcomes.addEventListener('change', () => {
        callbackRow.classList.toggle('hidden', selectedOutcome() !== 'callback_requested');
    });

    callLink.addEventListener('click', () => {
        if (!callTime.value) callTime.value = localInputValue(new Date());
        message.textContent = 'Dialer opened. Nothing is recorded until you choose how the call went and save.';
        setTimeout(() => (outcomes.querySelector('input:checked') || outcomeInputs()[0])?.focus(), 0);
    });

    copyPhone.addEventListener('click', async () => {
        if (!target?.phone) return;
        try {
            await navigator.clipboard.writeText(target.phone);
            message.textContent = 'Number copied. Log the call here after you dial.';
        } catch {
            callPhone.focus();
            callPhone.select();
            message.textContent = 'Copy failed. The number is selected in Number called; copy it manually.';
        }
    });

    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => selectTab(tab));
        tab.addEventListener('keydown', event => {
            const keys = {ArrowRight: 1, ArrowLeft: -1, Home: -index, End: tabs.length - 1 - index};
            if (!(event.key in keys)) return;
            event.preventDefault();
            selectTab(tabs[(index + keys[event.key] + tabs.length) % tabs.length], true);
        });
    });

    actions.forEach(button => {
        button.addEventListener('click', () => {
            if (save.disabled) return;
            select.value = button.dataset.outreachStatus;
            form.requestSubmit();
        });
    });
})();

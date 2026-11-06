const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'outreach.js'), 'utf8');

function setup() {
    const nodes = new Map(), requests = [], options = [];
    function element() {
        const classes = new Set();
        return {
            value: '', textContent: '', disabled: false, checked: false, children: [], listeners: {}, attributes: {},
            classList: {
                add: name => classes.add(name), contains: name => classes.has(name),
                toggle: (name, force) => (force ?? !classes.has(name)) ? classes.add(name) : classes.delete(name)
            },
            get parentElement() { return this._parent || (this._parent = element()); },
            setAttribute(name, value) { this.attributes[name] = value; },
            getAttribute(name) { return this.attributes[name]; },
            querySelectorAll: () => radios,
            querySelector: () => radios.find(radio => radio.checked) || null,
            addEventListener(type, handler) { this.listeners[type] = handler; },
            append(...children) { this.children.push(...children); },
            appendChild(child) { this.children.push(child); },
            replaceChildren() { this.children = []; },
            focus() { this.focused = true; },
            select() {},
            click() { return this.listeners.click?.(); },
            requestSubmit() { return nodes.get('outreach-form').listeners.submit({preventDefault() {}}); }
        };
    }
    const radios = ['no_answer', 'voicemail', 'connected', 'callback_requested', 'interested',
        'not_interested', 'wrong_number'].map(value => Object.assign(element(), {value}));
    const get = id => {
        if (!nodes.has(id)) nodes.set(id, element());
        return nodes.get(id);
    };
    get('outreach-tab-status').attributes['aria-controls'] = 'outreach-form';
    get('outreach-tab-call').attributes['aria-controls'] = 'outreach-call-form';
    const quick = ['awaiting_reply', 'replied', 'successful', 'failed'].map(status =>
        Object.assign(element(), {dataset: {outreachStatus: status}}));
    const state = {closed: false, refreshed: 0, status: 'not_contacted', history: [],
        calls: {call_attempts: 0, phone: '+1 (555) 010-2000', phone_source: 'listed', phone_invalid: false}};
    const context = vm.createContext({
        document: {getElementById: get, createElement: element, querySelectorAll: () => quick},
        window: {},
        navigator: {clipboard: {writeText: async text => { state.copied = text; }}},
        setTimeout: callback => callback(),
        openAccessibleModal(modal, config) { options.push(config); },
        closeAccessibleModal() { state.closed = true; },
        outreachLabel: value => value,
        async loadQueue() { state.refreshed++; },
        async fetch(url, config) {
            requests.push({url, config});
            if (state.error) throw new Error(state.error);
            if (config?.method === 'POST' && !url.endsWith('/calls')) {
                state.status = JSON.parse(config.body).status;
            }
            return {ok: !state.rejected, status: state.rejected ? 409 : 200, json: async () => state.rejected
                ? {status: 'error', message: state.rejected}
                : {status: 'success', outreach_status: state.status, calls: state.calls, history: state.history}};
        }
    });
    vm.runInContext(source, context);
    return {get, state, requests, options, quick, radios,
        open: context.window.openOutreachTracker,
        submit: () => get('outreach-form').requestSubmit(),
        submitCall: () => get('outreach-call-form').listeners.submit({preventDefault() {}})};
}

test('both lead types open owner API tracking and history stays plain text', async () => {
    for (const kind of ['prospects', 'jobs']) {
        const ui = setup();
        ui.state.history = [{previous_status: 'not_contacted', status: 'replied',
            note: '<img src=x onerror=alert(1)>', source: 'manual', created_at: '2026-11-04T03:00:00Z'}];
        await ui.open(kind, {id: 42, company_name: '<script>company</script>'});
        assert.equal(ui.requests[0].url, `/api/outreach/${kind}/42`);
        assert.equal(ui.get('outreach-lead-name').textContent, '<script>company</script>');
        assert.equal(ui.get('outreach-history').children[0].children[2].textContent,
            '<img src=x onerror=alert(1)>');
        assert.equal(ui.get('outreach-save').disabled, false);
    }
});

test('manual update includes expected status and optional note, refreshes queue without sending mail', async () => {
    const ui = setup();
    await ui.open('jobs', {id: 7, company_name: 'Example'});
    ui.get('outreach-next-status').value = 'awaiting_reply';
    ui.get('outreach-note').value = 'Sent from my own mailbox';
    await ui.submit();
    assert.deepEqual(JSON.parse(ui.requests[1].config.body), {
        status: 'awaiting_reply', expected_status: 'not_contacted', note: 'Sent from my own mailbox'
    });
    assert.equal(ui.requests[1].url, '/api/outreach/jobs/7');
    assert.equal(ui.state.refreshed, 1);
    assert.equal(ui.get('outreach-note').value, '');
    assert.equal(ui.get('outreach-message').textContent, 'Outreach update saved.');
    assert.equal(ui.options[1].closeOnEscape, false);
});

test('quick action submits selected outcome with drafted note', async () => {
    const ui = setup();
    await ui.open('prospects', {id: 1});
    ui.get('outreach-note').value = 'They replied';
    ui.quick[1].click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(JSON.parse(ui.requests[1].config.body).status, 'replied');
    assert.equal(JSON.parse(ui.requests[1].config.body).note, 'They replied');
});

test('failed save preserves optional note and shows error rather than success', async () => {
    const ui = setup();
    await ui.open('prospects', {id: 1});
    ui.get('outreach-note').value = 'Keep this note';
    ui.state.rejected = 'Outreach changed elsewhere';
    await ui.submit();
    assert.equal(ui.get('outreach-note').value, 'Keep this note');
    assert.match(ui.get('outreach-message').textContent, /Outreach changed elsewhere/);
    assert.equal(ui.state.refreshed, 0);
    assert.equal(ui.get('outreach-save').disabled, false);
});

test('history failure disables updates and closing works', async () => {
    const ui = setup();
    ui.state.error = 'Connection lost';
    await ui.open('jobs', {id: 1});
    assert.equal(ui.get('outreach-save').disabled, true);
    assert.match(ui.get('outreach-message').textContent, /Connection lost/);
    ui.get('outreach-close').click();
    assert.equal(ui.state.closed, true);
});

test('ambiguous save failure blocks retries to prevent duplicate notes', async () => {
    const ui = setup();
    await ui.open('prospects', {id: 1});
    ui.get('outreach-note').value = 'Keep this note';
    ui.state.error = 'Connection lost';
    await ui.submit();
    assert.equal(ui.get('outreach-save').disabled, true);
    assert.equal(ui.get('outreach-note').value, 'Keep this note');
    assert.match(ui.get('outreach-message').textContent, /save could not be confirmed/);
    await ui.submit();
    ui.quick[0].click();
    assert.equal(ui.requests.length, 2);
    ui.get('outreach-close').click();
    assert.equal(ui.state.closed, true);
    assert.equal(ui.get('outreach-prospects-1').focused, true);
});

test('standalone dialer: a call is logged without using the Call link and never auto-logged', async () => {
    const ui = setup();
    await ui.open('prospects', {id: 5, business_name: 'Cafe'}, {tab: 'call'});
    assert.equal(ui.get('outreach-tab-call').attributes['aria-selected'], 'true');
    assert.equal(ui.get('outreach-call-form').classList.contains('hidden'), false);
    assert.equal(ui.get('outreach-form').classList.contains('hidden'), true);
    assert.equal(ui.get('outreach-call-link').href, 'tel:+15550102000');
    assert.equal(ui.get('outreach-call-phone').value, '+1 (555) 010-2000');
    ui.get('outreach-call-link').click();
    assert.equal(ui.requests.length, 1, 'Opening the dialer must not record anything');
    assert.match(ui.get('outreach-message').textContent, /Nothing is recorded/);
    assert.equal(ui.radios[0].focused, true);
    await ui.get('outreach-copy-phone').click();
    assert.equal(ui.state.copied, '+1 (555) 010-2000');
    ui.get('outreach-call-phone').value = ' +1 555 0999 ';
    ui.get('outreach-call-time').value = '2026-11-04T09:30';
    ui.radios[3].checked = true;
    ui.get('outreach-call-outcomes').listeners.change();
    assert.equal(ui.get('outreach-callback-row').classList.contains('hidden'), false);
    ui.get('outreach-callback-at').value = '2026-11-06T10:00';
    ui.get('outreach-call-note').value = 'Call back Friday';
    await ui.submitCall();
    const request = ui.requests[1];
    assert.equal(request.url, '/api/outreach/prospects/5/calls');
    assert.deepEqual(JSON.parse(request.config.body), {
        outcome: 'callback_requested', expected_status: 'not_contacted', note: 'Call back Friday',
        occurred_at: new Date('2026-11-04T09:30').toISOString(), phone: '+1 555 0999',
        callback_at: new Date('2026-11-06T10:00').toISOString()
    });
    assert.equal(ui.get('outreach-message').textContent, 'Call logged.');
    assert.equal(ui.state.refreshed, 1);
    assert.equal(ui.get('outreach-call-note').value, '');
    assert.equal(ui.radios[3].checked, false);
});

test('call requires an outcome, omits callback for other outcomes, and blocks ambiguous retries', async () => {
    const ui = setup();
    await ui.open('jobs', {id: 8, company_name: 'Co'});
    await ui.submitCall();
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.get('outreach-message').textContent, 'Choose how the call went.');
    ui.get('outreach-callback-at').value = '2026-11-06T10:00';
    ui.radios[0].checked = true;
    ui.get('outreach-call-note').value = 'Rang twice';
    ui.state.error = 'Connection lost';
    await ui.submitCall();
    const body = JSON.parse(ui.requests[1].config.body);
    assert.equal(body.outcome, 'no_answer');
    assert.equal('callback_at' in body, false);
    assert.equal(ui.get('outreach-call-save').disabled, true);
    assert.equal(ui.get('outreach-call-note').value, 'Rang twice');
    assert.match(ui.get('outreach-message').textContent, /could not be confirmed/);
    await ui.submitCall();
    assert.equal(ui.requests.length, 2);
});

test('call history and invalid numbers render as plain text with call details', async () => {
    const ui = setup();
    ui.state.calls = {call_attempts: 2, phone: '<b>555</b>', phone_source: 'manual', phone_invalid: true};
    ui.state.history = [{previous_status: 'not_contacted', status: 'not_contacted', channel: 'call',
        call_outcome: 'wrong_number', phone: '<b>555</b>', note: '<i>x</i>', source: 'call',
        occurred_at: '2026-11-04T03:00:00Z', created_at: '2026-11-04T04:00:00Z'}];
    await ui.open('jobs', {id: 2});
    const item = ui.get('outreach-history').children[0];
    assert.match(item.children[0].textContent, /^Call · Wrong or invalid number · Status unchanged/);
    assert.match(item.children[1].textContent, /<b>555<\/b>/);
    assert.equal(item.children[2].textContent, '<i>x</i>');
    assert.match(ui.get('outreach-phone-source').textContent, /Entered manually · Flagged as wrong or invalid · 2 calls logged/);
    assert.equal(ui.get('outreach-phone-display').parentElement.classList.contains('outreach-phone-invalid'), true);
});

test('tabs support arrow keys and missing numbers hide dialer actions', async () => {
    const ui = setup();
    ui.state.calls = {call_attempts: 0, phone: null, phone_source: null, phone_invalid: false};
    await ui.open('prospects', {id: 3});
    assert.equal(ui.get('outreach-tab-status').attributes['aria-selected'], 'true');
    assert.equal(ui.get('outreach-call-link').classList.contains('hidden'), true);
    assert.equal(ui.get('outreach-copy-phone').classList.contains('hidden'), true);
    assert.equal(ui.get('outreach-call-phone').placeholder, 'Enter the number you dialed');
    ui.get('outreach-tab-status').listeners.keydown({key: 'ArrowRight', preventDefault() {}});
    assert.equal(ui.get('outreach-tab-call').attributes['aria-selected'], 'true');
    assert.equal(ui.get('outreach-tab-call').focused, true);
    assert.equal(ui.get('outreach-tab-status').tabIndex, -1);
});

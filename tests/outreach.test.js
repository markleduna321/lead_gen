const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'outreach.js'), 'utf8');

function setup() {
    const nodes = new Map(), requests = [], options = [];
    function element() {
        return {
            value: '', textContent: '', disabled: false, children: [], listeners: {},
            addEventListener(type, handler) { this.listeners[type] = handler; },
            append(...children) { this.children.push(...children); },
            appendChild(child) { this.children.push(child); },
            replaceChildren() { this.children = []; },
            focus() { this.focused = true; },
            click() { return this.listeners.click?.(); },
            requestSubmit() { return nodes.get('outreach-form').listeners.submit({preventDefault() {}}); }
        };
    }
    const get = id => {
        if (!nodes.has(id)) nodes.set(id, element());
        return nodes.get(id);
    };
    const quick = ['awaiting_reply', 'replied', 'successful', 'failed'].map(status =>
        Object.assign(element(), {dataset: {outreachStatus: status}}));
    const state = {closed: false, refreshed: 0, status: 'not_contacted', history: []};
    const context = vm.createContext({
        document: {getElementById: get, createElement: element, querySelectorAll: () => quick},
        window: {},
        openAccessibleModal(modal, config) { options.push(config); },
        closeAccessibleModal() { state.closed = true; },
        outreachLabel: value => value,
        async loadQueue() { state.refreshed++; },
        async fetch(url, config) {
            requests.push({url, config});
            if (state.error) throw new Error(state.error);
            if (config?.method === 'POST') {
                state.status = JSON.parse(config.body).status;
            }
            return {ok: !state.rejected, status: state.rejected ? 409 : 200, json: async () => state.rejected
                ? {status: 'error', message: state.rejected}
                : {status: 'success', outreach_status: state.status, history: state.history}};
        }
    });
    vm.runInContext(source, context);
    return {get, state, requests, options, quick,
        open: context.window.openOutreachTracker,
        submit: () => get('outreach-form').requestSubmit()};
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

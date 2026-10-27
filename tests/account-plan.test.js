const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'account_plan.js'), 'utf8');
function element(value) {
    return {
        value, children: [], disabled: false, checked: true, hidden: true, textContent: '',
        attributes: {}, classList: {add() {}},
        appendChild(child) { this.children.push(child); },
        setAttribute(key, value) { this.attributes[key] = value; },
        getAttribute(key) { return this.attributes[key]; },
        querySelectorAll() { return this.controls || []; }
    };
}
async function setup({team = false, enterprise = false, fail = false, sufficient = true, card = false} = {}) {
    const sources = ['google', 'yelp', 'facebook', 'instagram', 'linkedin'].map(value => {
        const input = element(value);
        input.parentElement = element();
        return input;
    });
    const ids = new Map([
        ['account-plan-status', element()], ['btn-discovery-submit', element()],
        ['nav-user-management', element()], ['nav-billing', element()], ['discovery-tab-jobs', element()],
        ['custom-service-editor', element()], ['discovery-panel-jobs', element()],
        ['job-pages-input', element()], ['plan-discovery-policy', element()]
    ]);
    if (card) ['sidebar-credits', 'credit-plan', 'credit-balance', 'credit-usage-values',
        'credit-usage-percent', 'credit-usage-meter', 'credit-ring-value', 'credit-renewal',
        'credit-live-status', 'credit-plan-link'].forEach(id => {
        const node = element();
        node.dataset = {};
        ids.set(id, node);
    });
    const editor = element(), custom = element();
    ids.get('custom-service-editor').controls = [editor];
    ids.get('discovery-panel-jobs').controls = [];
    ids.get('job-pages-input').options = [1, 2, 3, 5].map(value => element(String(value)));
    ids.get('discovery-tab-jobs').attributes['aria-selected'] = 'false';
    const calls = [], prompts = [], errors = [], storage = new Map();
    const events = {}, timers = [];
    const window = {addEventListener(name, callback) { events[name] = callback; }};
    const context = vm.createContext({
        window, console, AbortSignal, crypto: {randomUUID: () => 'test-request-key'},
        setInterval(callback, delay) { timers.push({callback, delay}); },
        confirm(text) { prompts.push(text); return true; },
        showDiscoveryError: text => errors.push(text),
        sessionStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key)
        },
        document: {
            addEventListener(name, callback) { events[name] = callback; },
            body: {dataset: {page: 'discovery'}},
            getElementById: id => ids.get(id) || null,
            createElement: () => element(),
            querySelector: () => ({content: 'csrf-token'}),
            querySelectorAll(selector) {
                if (selector === 'input[name="scan-source"]') return sources;
                if (selector === '#custom-hiring-services input') return [custom];
                if (selector.startsWith('input[name="scan-source"],')) return [...sources, editor, ids.get('btn-discovery-submit')];
                if (selector.startsWith('#custom-service-editor input,')) return [editor];
                return [];
            }
        },
        async fetch(url, options) {
            calls.push({url, options});
            if (url === '/api/account-plan') return {
                ok: !fail, json: async () => fail ? {message: 'Database offline'} : {
                    status: 'success',
                    account: {balance: '750', owner: true, can_manage_team: team,
                        usage: {used: '250', total: '1000', percent: '25.0',
                            next_date: '2026-11-27T00:00:00Z', next_event: 'expiry'},
                        plan: {
                        id: enterprise ? 'enterprise' : team ? 'small_team' : 'solo',
                        name: team ? 'Small Team' : 'Solo', active: true,
                        sources: team ? sources.map(input => input.value) : ['google', 'yelp'],
                        custom_services: enterprise, hiring_pages: enterprise ? 5 : team ? 3 : 0,
                        industries: team ? 5 : 3
                    }}
                }
            };
            return {ok: true, json: async () => ({status: 'success', quote: {
                charge: '30', balance: '750', sufficient,
                zero_result_refund: '21.00', policy: '70% refund if zero new leads'
            }})};
        }
    });
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    return {context, window, ids, sources, editor, custom, calls, prompts, errors, storage, events, timers};
}

test('Solo keeps source and hiring upgrade controls visible but locked, with no owner navigation', async () => {
    const ui = await setup();
    assert.deepEqual(ui.sources.map(input => input.disabled), [false, false, true, true, true]);
    assert.equal(ui.editor.disabled, true);
    assert.equal(ui.custom.disabled, true);
    assert.equal(ui.ids.get('nav-user-management').hidden, true);
    assert.match(ui.ids.get('discovery-panel-jobs').children[0].textContent, /Upgrade to Small Team/);
    ui.ids.get('discovery-tab-jobs').attributes['aria-selected'] = 'true';
    ui.window.applyAccountCapabilities();
    assert.equal(ui.ids.get('btn-discovery-submit').disabled, true);
});

test('Small Team owner sees management, preset hiring and locked custom editor; Enterprise unlocks it', async () => {
    const ui = await setup({team: true});
    assert.equal(ui.ids.get('nav-user-management').hidden, false);
    assert.equal(ui.sources.some(input => input.disabled), false);
    assert.equal(ui.editor.disabled, true);
    assert.equal(ui.ids.get('job-pages-input').options[3].disabled, true);
    const enterprise = await setup({team: true, enterprise: true});
    assert.equal(enterprise.editor.disabled, false);
    assert.equal(enterprise.custom.disabled, false);
    assert.equal(enterprise.ids.get('job-pages-input').options[3].disabled, false);
});

test('Search confirmation uses a server quote, CSRF, refund disclosure and stable replay key', async () => {
    const ui = await setup();
    const payload = {categories: ['A', 'B', 'C'], sources: ['google']};
    assert.equal(await ui.window.preparePaidSearch('/api/trigger/discover', payload), true);
    const quote = ui.calls.find(call => call.url === '/api/discovery/quote');
    assert.equal(quote.options.headers['X-CSRF-Token'], 'csrf-token');
    assert.match(ui.prompts[0], /30 shared search credits/);
    assert.match(ui.prompts[0], /21.00 credits/);
    assert.equal(ui.window.paidDiscoveryHeaders['Idempotency-Key'], 'test-request-key');
    assert.equal(await ui.window.preparePaidSearch('/api/trigger/discover', payload), true);
    assert.equal(ui.window.paidDiscoveryHeaders['Idempotency-Key'], 'test-request-key');
    ui.window.completePaidSearchRequest();
    assert.equal(ui.storage.size, 0);
});

test('Unavailable plan or insufficient balance blocks search and surfaces errors', async () => {
    const unavailable = await setup({fail: true});
    assert.equal(unavailable.ids.get('btn-discovery-submit').disabled, true);
    assert.match(unavailable.ids.get('account-plan-status').textContent, /Database offline/);
    assert.equal(await unavailable.window.preparePaidSearch('/api/trigger/discover', {}), false);
    const insufficient = await setup({sufficient: false});
    assert.equal(await insufficient.window.preparePaidSearch('/api/trigger/discover', {}), false);
    assert.equal(insufficient.prompts.length, 0);
    assert.match(insufficient.errors[0], /costs 30 credits/);
});

test('Balance refresh shows settled credits and reports refresh failures explicitly', async () => {
    const ui = await setup();
    ui.context.fetch = async () => ({ok: true, json: async () => ({
        status: 'success', account: {balance: '747.00', can_manage_team: false,
            plan: {name: 'Solo', active: true, custom_services: false, hiring_pages: 0}}
    })});
    await ui.window.refreshAccountPlan();
    assert.match(ui.ids.get('account-plan-status').textContent, /747.00 shared search credits/);
    ui.context.fetch = async () => { throw new Error('Database offline'); };
    await ui.window.refreshAccountPlan();
    assert.match(ui.ids.get('account-plan-status').textContent, /Balance refresh failed: Database offline/);
    assert.equal(ui.ids.get('account-plan-status').attributes.role, 'alert');
});

test('Sidebar renders real balance, usage percentage, expiry and upgrade link', async () => {
    const ui = await setup({card: true});
    assert.equal(ui.ids.get('credit-balance').textContent, '750');
    assert.equal(ui.ids.get('credit-usage-values').textContent, '250 / 1,000 used');
    assert.equal(ui.ids.get('credit-usage-percent').textContent, '25.0%');
    assert.equal(ui.ids.get('credit-usage-meter').value, 25);
    assert.equal(ui.ids.get('credit-ring-value').attributes['stroke-dasharray'], '75 100');
    assert.match(ui.ids.get('credit-renewal').textContent, /Plan expires/);
    assert.equal(ui.ids.get('credit-plan-link').textContent, 'Upgrade plan');
    assert.equal(ui.ids.get('credit-plan-link').href, '/billing');
    assert.equal(ui.ids.get('nav-billing').hidden, false);
    assert.equal(ui.ids.get('sidebar-credits').attributes['aria-busy'], 'false');
    ui.context.fetch = async () => { throw new Error('Offline'); };
    await ui.window.refreshAccountPlan();
    assert.equal(ui.ids.get('sidebar-credits').dataset.state, 'error');
    assert.match(ui.ids.get('credit-live-status').textContent, /Wallet unavailable: Offline/);
    assert.equal(ui.ids.get('credit-balance').textContent, '750');
});

test('Live wallet polls only visible tabs and refreshes on focus and tab return', async () => {
    const ui = await setup({card: true});
    assert.equal(ui.timers[0].delay, 30000);
    const initial = ui.calls.length;
    ui.context.document.hidden = true;
    ui.timers[0].callback();
    assert.equal(ui.calls.length, initial);
    ui.context.document.hidden = false;
    ui.events.focus();
    ui.events.visibilitychange();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ui.calls.length, initial + 1);
    assert.equal(ui.calls.at(-1).options.cache, 'no-store');
    ui.timers[0].callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ui.calls.length, initial + 2);
});

test('Sidebar handles exhausted, inactive and Enterprise wallets without inventing a renewal', async () => {
    const ui = await setup({card: true});
    const account = {balance: '0.00', owner: true, can_manage_team: true,
        plan: {id: 'enterprise', name: 'Enterprise', active: true, hiring_pages: 5, custom_services: true},
        usage: {used: '7500.00', total: '7500.00', percent: '100.0',
            next_date: '2026-11-27T00:00:00Z', next_event: 'refresh'}};
    ui.context.fetch = async () => ({ok: true, json: async () => ({status: 'success', account})});
    await ui.window.refreshAccountPlan();
    assert.equal(ui.ids.get('credit-usage-meter').value, 100);
    assert.equal(ui.ids.get('credit-balance').textContent, '0');
    assert.equal(ui.ids.get('sidebar-credits').dataset.state, 'low');
    assert.equal(ui.ids.get('credit-plan-link').textContent, 'View plan details');
    assert.match(ui.ids.get('credit-renewal').textContent, /Credits refresh/);
    account.plan.active = false;
    account.usage = {used: '0', total: '0', percent: '0.0', next_date: null, next_event: 'expiry'};
    await ui.window.refreshAccountPlan();
    assert.equal(ui.ids.get('credit-usage-percent').textContent, '0.0%');
    assert.equal(ui.ids.get('credit-ring-value').attributes['stroke-dasharray'], '0 100');
    assert.equal(ui.ids.get('credit-plan-link').textContent, 'Choose a plan');
    assert.match(ui.ids.get('credit-renewal').textContent, /Activate a plan/);
});

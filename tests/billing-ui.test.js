const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'billing.js'), 'utf8');
const prices = {
    solo: {1: 99900, 12: 999000, topup: 49900, pack: 350},
    small_team: {1: 249900, 12: 2499000, topup: 79900, pack: 750},
    enterprise: {1: 599900, 12: 5999000, topup: 149900, pack: 2000}
};
function element() {
    return {
        textContent: '', children: [], dataset: {}, listeners: {}, value: '1', disabled: false,
        appendChild(node) { this.children.push(node); },
        replaceChildren() { this.children = []; },
        addEventListener(event, callback) { this.listeners[event] = callback; }
    };
}
function order(overrides = {}) {
    return {id: 'order-1', kind: 'plan', plan_id: 'solo', months: 1, amount: 99900,
        status: 'pending', checkout_url: 'https://checkout.paymongo.com/cs_test',
        created_at: '2026-10-27T00:00:00Z', livemode: false, ...overrides};
}
function setup({active = false, ready = true, orders = [], search = '', can_start_trial = false} = {}) {
    const state = {ready, orders, can_start_trial, account: {owner: true, balance: '750.00',
        plan: {id: active ? 'solo' : 'unassigned', name: active ? 'Solo' : 'No active plan', active},
        ends_at: active ? '2026-11-27T00:00:00Z' : null}};
    const ids = new Map();
    const get = id => {
        if (!ids.has(id)) ids.set(id, element());
        return ids.get(id);
    };
    get('billing-data').textContent = JSON.stringify(state);
    get('billing-prices').textContent = JSON.stringify(prices);
    const buttons = Object.keys(prices).map(plan => Object.assign(element(), {dataset: {buyPlan: plan}}));
    const priceNodes = Object.keys(prices).map(plan => Object.assign(element(), {dataset: {pricePlan: plan}}));
    const notes = Object.keys(prices).map(plan => Object.assign(element(), {dataset: {priceNote: plan}}));
    const calls = [], prompts = [], redirects = [], storage = new Map();
    const window = {location: {search, assign(url) { redirects.push(url); }}, refreshAccountPlan: async () => {}};
    const context = vm.createContext({
        window, URL, URLSearchParams, AbortSignal,
        crypto: {randomUUID: () => 'stable-purchase-key'},
        confirm(text) { prompts.push(text); return true; },
        sessionStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key)
        },
        document: {
            getElementById: get, createElement: element,
            querySelector: () => ({content: 'purchase-csrf'}),
            querySelectorAll(selector) {
                if (selector === '[data-buy-plan]') return buttons;
                if (selector === '[data-price-plan]') return priceNodes;
                if (selector === '[data-price-note]') return notes;
                return [];
            }
        },
        async fetch(url, options) {
            calls.push({url, options});
            let result;
            if (url === '/api/billing/checkout') result = {order: order()};
            else if (url === '/api/billing') result = {billing: state};
            else result = {order: orders[0] || order()};
            return {ok: true, json: async () => ({status: 'success', ...result})};
        }
    });
    vm.runInContext(source, context);
    return {context, state, ids, get, buttons, priceNodes, notes, calls, prompts, redirects, storage};
}

test('Purchase page displays monthly and annual PHP prices and blocks active plan replacement', () => {
    const ui = setup();
    assert.match(ui.priceNodes[0].textContent, /999.*month/);
    ui.get('billing-months').value = '12';
    ui.get('billing-months').listeners.change();
    assert.match(ui.priceNodes[0].textContent, /9,990.*year/);
    assert.match(ui.notes[0].textContent, /Credits granted monthly/);
    assert.equal(ui.buttons.some(button => button.disabled), false);
    const active = setup({active: true});
    assert.equal(active.buttons.every(button => button.disabled), true);
    assert.equal(active.get('billing-buy-topup').disabled, false);
    assert.match(active.get('billing-buy-topup').textContent, /350.*499/);
    assert.equal(setup({ready: false}).buttons.every(button => button.disabled), true);
});

test('Checkout sends server-owned product selectors, CSRF and a stable replay key then redirects', async () => {
    const ui = setup();
    await ui.buttons[0].listeners.click();
    const request = ui.calls.find(call => call.url === '/api/billing/checkout');
    assert.deepEqual(JSON.parse(request.options.body), {kind: 'plan', plan_id: 'solo', months: 1});
    assert.equal(request.options.headers['X-CSRF-Token'], 'purchase-csrf');
    assert.equal(request.options.headers['Idempotency-Key'], 'stable-purchase-key');
    assert.match(ui.prompts[0], /no automatic renewal/);
    assert.deepEqual(ui.redirects, ['https://checkout.paymongo.com/cs_test']);
    assert.equal(ui.storage.size, 1);
});

test('Top-up purchase uses current plan and never takes a client-entered price', async () => {
    const ui = setup({active: true});
    await ui.get('billing-buy-topup').listeners.click();
    assert.deepEqual(JSON.parse(ui.calls[0].options.body), {kind: 'topup', plan_id: 'solo', months: 1});
    assert.match(ui.prompts[0], /499/);
});

test('Pending orders block new purchases and expose only a trusted resume URL', () => {
    const ui = setup({orders: [order()]});
    assert.equal(ui.buttons.every(button => button.disabled), true);
    const actions = ui.get('billing-orders').children[0].children.at(-1);
    assert.equal(actions.children[0].href, 'https://checkout.paymongo.com/cs_test');
    const bad = setup({orders: [order({checkout_url: 'https://evil.example/steal'})]});
    const badActions = bad.get('billing-orders').children[0].children.at(-1);
    assert.equal(badActions.children.some(node => node.href), false);
});

test('Success return rechecks PayMongo and never treats the URL as proof of payment', async () => {
    const ui = setup({orders: [order()], search: '?order=order-1&checkout=success'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ui.calls[0].url, '/api/billing/orders/order-1/recheck');
    assert.equal(ui.calls[0].options.method, 'POST');
    assert.match(ui.get('billing-message').textContent, /not confirmed yet/);
    assert.doesNotMatch(ui.get('billing-message').textContent, /now active/);
});

test('Checkout failures are visible, preserve the replay key and refresh history before retry', async () => {
    const ui = setup();
    ui.context.fetch = async (url) => {
        if (url === '/api/billing') return {ok: true, json: async () => ({status: 'success',
            billing: {...ui.state, orders: [order({status: 'unknown'})]}})};
        throw new Error('Checkout timed out');
    };
    await ui.buttons[0].listeners.click();
    assert.equal(ui.redirects.length, 0);
    assert.match(ui.get('billing-message').textContent, /Checkout timed out/);
    assert.equal(ui.storage.size, 1);
    assert.equal(ui.buttons.every(button => button.disabled), true);
    assert.match(ui.get('billing-orders').children[0].children[3].textContent, /Needs reconciliation/);
});

test('Cancellation waits for explicit confirmation and a provider-backed response', async () => {
    const ui = setup({orders: [order()]});
    const cancel = ui.get('billing-orders').children[0].children.at(-1).children.at(-1);
    await cancel.listeners.click();
    assert.match(ui.prompts[0], /PayMongo may refuse cancellation/);
    assert.equal(ui.calls[0].url, '/api/billing/orders/order-1/cancel');
    assert.equal(ui.calls[0].options.method, 'POST');
});

test('Eligible legacy accounts can claim a free trial without checkout or payment configuration', async () => {
    const ui = setup({ready: false, can_start_trial: true});
    assert.equal(ui.get('billing-trial-offer').hidden, false);
    assert.equal(ui.get('billing-start-trial').disabled, false);
    ui.context.fetch = async (url, options) => {
        ui.calls.push({url, options});
        if (url === '/api/billing/trial') {
            ui.state.can_start_trial = false;
            ui.state.account.plan = {id: 'trial', name: 'Free Trial', active: true};
            ui.state.account.balance = '30';
            return {ok: true, json: async () => ({status: 'success'})};
        }
        return {ok: true, json: async () => ({status: 'success', billing: ui.state})};
    };
    await ui.get('billing-start-trial').listeners.click();
    assert.equal(ui.calls[0].url, '/api/billing/trial');
    assert.equal(ui.calls[0].options.headers['X-CSRF-Token'], 'purchase-csrf');
    assert.equal(ui.get('billing-trial-offer').hidden, true);
    assert.match(ui.get('billing-current-balance').textContent, /30 shared/);
    assert.equal(ui.redirects.length, 0);
});

test('Trial failures are explicit and restore the claim button without payment warnings', async () => {
    const ui = setup({ready: false, can_start_trial: true});
    assert.equal(setup().get('billing-trial-offer').hidden, true);
    ui.context.fetch = async url => url === '/api/billing/trial'
        ? {ok: false, json: async () => ({status: 'error', message: 'Trial unavailable'})}
        : {ok: true, json: async () => ({status: 'success', billing: ui.state})};
    await ui.get('billing-start-trial').listeners.click();
    assert.match(ui.get('billing-message').textContent, /Trial unavailable/);
    assert.doesNotMatch(ui.get('billing-message').textContent, /payment|purchase/);
    assert.equal(ui.get('billing-start-trial').disabled, false);
});

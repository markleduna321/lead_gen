const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'src', 'templates', 'dashboard.html'), 'utf8');
const script = [...template.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(source => source.includes('function loadOverview'))
    .replace('{{ carto_basemap_api_key | tojson }}', '""')
    .replace('{{ hiring_builtin_services | tojson }}', '[]');
const themeScript = fs.readFileSync(path.join(root, 'static', 'workspace.js'), 'utf8');

function mockElement() {
    const classes = new Set();
    return {
        dataset: {}, style: {}, children: [], attributes: {}, value: '',
        classList: {
            add: key => classes.add(key), remove: key => classes.delete(key),
            contains: key => classes.has(key),
            toggle(key, force) { if (force) classes.add(key); else classes.delete(key); }
        },
        setAttribute(key, value) { this.attributes[key] = value; },
        appendChild(child) { this.children.push(child); },
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; }
    };
}

test('appearance follows system, supports overrides, and persists preference', () => {
    const elements = new Map();
    const root = mockElement();
    let stored = null;
    let systemChanged;
    const media = { matches: true, addEventListener(type, handler) { systemChanged = handler; } };
    const context = vm.createContext({
        window: { matchMedia: () => media }, console,
        localStorage: { getItem: () => stored, setItem(key, value) { stored = value; } },
        document: {
            documentElement: root, body: mockElement(), addEventListener() {},
            getElementById(id) {
                if (!elements.has(id)) elements.set(id, mockElement());
                return elements.get(id);
            }
        }
    });
    vm.runInContext(themeScript, context);
    assert.equal(root.dataset.theme, 'dark');
    context.setThemePreference('light');
    assert.equal(root.dataset.theme, 'light');
    assert.equal(stored, 'light');
    media.matches = false;
    systemChanged();
    assert.equal(root.dataset.theme, 'light');
    context.setThemePreference('system');
    media.matches = true;
    systemChanged();
    assert.equal(root.dataset.theme, 'dark');
    context.toggleSidebar(true);
    assert.equal(elements.get('menu-toggle').attributes['aria-expanded'], 'true');
    context.toggleSidebar(false);
    assert.equal(elements.get('sidebar-backdrop').hidden, true);
});

test('public pages share theme persistence and Escape needs no sidebar', () => {
    const root = mockElement();
    const selector = mockElement();
    const handlers = {};
    const stored = new Map([['asuratech-theme', 'dark']]);
    const context = vm.createContext({
        window: { matchMedia: () => ({ matches: false, addEventListener() {} }) }, console,
        localStorage: { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value) },
        document: {
            documentElement: root, body: { dataset: { page: 'home' } },
            getElementById: id => id === 'theme-preference' ? selector : null,
            addEventListener: (event, handler) => { handlers[event] = handler; }
        }
    });
    vm.runInContext(themeScript, context);
    handlers.DOMContentLoaded();
    assert.equal(root.dataset.theme, 'dark');
    assert.equal(selector.value, 'dark');
    assert.doesNotThrow(() => handlers.keydown({ key: 'Escape' }));
    context.setThemePreference('light');
    assert.equal(stored.get('asuratech-theme'), 'light');
    assert.equal(root.dataset.theme, 'light');
});

function overviewContext(fail = false) {
    const elements = new Map();
    const document = {
        body: { dataset: { page: 'overview' } },
        createElement: mockElement,
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, mockElement());
            return elements.get(id);
        }
    };
    const context = vm.createContext({
        document, window: {}, URLSearchParams, console: { error() {} },
        fetch: async url => ({
            ok: !fail,
            json: async () => fail ? { status: 'error', message: 'Database unavailable' }
                : url.startsWith('/api/queue')
                    ? { status: 'success', leads: [{ business_name: '<Test>', category: 'Cafe', status: 'discovered' }],
                        metrics: { global_total: 10, global_pending: 4, global_sent: 3, global_followup: 2 } }
                    : { status: 'success', leads: [], metrics: { total: 5, pending: 2, sent: 1, with_email: 3 } }
        })
    });
    vm.runInContext(script, context);
    return { context, elements };
}

test('overview aggregates real API metrics and renders recent records as text', async () => {
    const { context, elements } = overviewContext();
    await context.loadOverview();
    assert.equal(elements.get('overview-leads').innerText, '15');
    assert.equal(elements.get('overview-review').innerText, '6');
    assert.equal(elements.get('overview-sent').innerText, '4');
    assert.equal(elements.get('overview-followup').innerText, '2');
    assert.equal(elements.get('pipeline-jobs-bar').style.width, `${5 / 15 * 100}%`);
    const nameCell = elements.get('overview-recent-prospects').children[0].children[0];
    assert.equal(nameCell.children[0].textContent, '<Test>');
    assert.match(elements.get('overview-recent-jobs').children[0].children[0].textContent, /No leads yet/);
});

test('overview failure is explicit and never displays success-shaped zero metrics', async () => {
    const { context, elements } = overviewContext(true);
    await context.loadOverview();
    assert.equal(elements.get('overview-leads').innerText, '—');
    assert.match(elements.get('overview-error-message').innerText, /Database unavailable/);
    assert.equal(elements.get('overview-error').classList.contains('hidden'), false);
});

test('a failed overview refresh clears previously loaded pipeline values', async () => {
    const { context, elements } = overviewContext();
    await context.loadOverview();
    assert.equal(elements.get('pipeline-jobs-count').innerText, '5');
    context.fetch = async () => ({
        ok: false,
        json: async () => ({ status: 'error', message: 'Database unavailable' })
    });
    await context.loadOverview();
    for (const key of ['prospects', 'jobs', 'review', 'sent']) {
        assert.equal(elements.get(`pipeline-${key}-count`).innerText, '—');
        assert.equal(elements.get(`pipeline-${key}-bar`).style.width, '0%');
    }
    assert.match(elements.get('overview-recent-prospects').children[0].children[0].textContent, /Data unavailable/);
});

const PAYLOAD = '<img src=x onerror="globalThis.pwned=1">';

function htmlTrackingContext() {
    const elements = new Map();
    const htmlWrites = [];
    function trackedElement() {
        const el = mockElement();
        let html = '';
        Object.defineProperty(el, 'innerHTML', {
            get: () => html,
            set(value) { html = value; if (value) htmlWrites.push(value); }
        });
        return el;
    }
    const document = {
        body: { dataset: { page: 'prospects' } },
        createElement: trackedElement,
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, trackedElement());
            return elements.get(id);
        },
        querySelectorAll: () => []
    };
    const context = vm.createContext({ document, window: {}, URLSearchParams, console });
    vm.runInContext(script, context);
    return { context, elements, htmlWrites };
}

test('scraped lead fields render as text and never as HTML', () => {
    const { context, elements, htmlWrites } = htmlTrackingContext();
    context.lead = { id: 7, business_name: PAYLOAD, category: PAYLOAD, status: PAYLOAD,
        email: 'a@example.test', lead_score: 80, source_platform: 'google_maps' };
    vm.runInContext("activeQueueView = 'prospects'; currentLeads = [lead]; renderTable();", context);
    const cells = elements.get('table-body').children[0].children;
    assert.equal(cells[0].textContent, PAYLOAD);
    assert.equal(cells[0].children[0].title, 'Email found');
    assert.equal(cells[1].textContent, PAYLOAD);
    assert.equal(cells[3].children[0].textContent, '80');
    assert.equal(cells[4].textContent, PAYLOAD);
    assert.ok(htmlWrites.every(value => !value.includes('onerror')), htmlWrites.join('\n'));
    assert.match(cells[2].innerHTML, /G Maps/);
});

test('known statuses keep their badge and prototype keys are treated as text', () => {
    const { context, elements } = htmlTrackingContext();
    context.leads = [{ id: 1, business_name: 'A', status: 'sent' }, { id: 2, business_name: 'B', status: 'constructor' }];
    vm.runInContext("activeQueueView = 'prospects'; currentLeads = leads; renderTable();", context);
    const rows = elements.get('table-body').children;
    assert.match(rows[0].children[4].innerHTML, />Sent</);
    assert.equal(rows[1].children[4].textContent, 'constructor');
});

test('custom business types are added as text and capped at 60 characters', () => {
    const { context, elements, htmlWrites } = htmlTrackingContext();
    context.document.getElementById('custom-biz-input').value = `  ${PAYLOAD}${'y'.repeat(80)}  `;
    context.addCustomBizType();
    const [checkbox, , text] = elements.get('custom-biz-tags').children[0].children;
    assert.equal(checkbox.value.length, 60);
    assert.equal(checkbox.value, `${PAYLOAD}${'y'.repeat(80)}`.slice(0, 60));
    assert.equal(text.textContent, checkbox.value);
    assert.equal(checkbox.checked, true);
    assert.equal(htmlWrites.length, 0);
    assert.equal(elements.get('custom-biz-input').value, '');
});

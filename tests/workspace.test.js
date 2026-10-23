const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'src', 'templates', 'dashboard.html'), 'utf8');
const script = [...template.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(source => source.includes('function loadOverview'))
    .replace('{{ carto_basemap_api_key | tojson }}', '""');
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

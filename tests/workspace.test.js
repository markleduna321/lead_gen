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
        dataset: {}, style: {}, children: [], attributes: {}, listeners: {}, value: '',
        classList: {
            add: key => classes.add(key), remove: key => classes.delete(key),
            contains: key => classes.has(key),
            toggle(key, force) { if (force) classes.add(key); else classes.delete(key); }
        },
        setAttribute(key, value) { this.attributes[key] = value; },
        addEventListener(type, handler) {
            this.listeners[type] = handler;
        },
        removeEventListener(type, handler) {
            if (this.listeners[type] === handler) delete this.listeners[type];
        },
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
        window: {
            matchMedia(query) {
                return query === '(prefers-color-scheme: dark)' ? media : { matches: false };
            }
        }, console,
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
    assert.equal(elements.get('theme-color-meta').content, '#101619');
    context.setThemePreference('light');
    assert.equal(root.dataset.theme, 'light');
    assert.equal(elements.get('theme-color-meta').content, '#f5f7f9');
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

test('auth autofocus is limited to fine-pointer devices', () => {
    function focusedCount(hasFinePointer) {
        const listeners = {};
        let focused = 0;
        const autofocus = {focus() { focused += 1; }};
        const context = vm.createContext({
            window: {
                matchMedia(query) {
                    return {
                        matches: query === '(hover: hover) and (pointer: fine)' && hasFinePointer,
                        addEventListener() {}
                    };
                }
            },
            document: {
                documentElement: mockElement(),
                body: mockElement(),
                addEventListener(type, handler) { listeners[type] = handler; },
                getElementById() { return null; },
                querySelector(selector) {
                    return selector === '[data-autofocus]' ? autofocus : null;
                }
            },
            localStorage: {getItem: () => null, setItem() {}},
            console
        });
        vm.runInContext(themeScript, context);
        listeners.DOMContentLoaded();
        return focused;
    }

    assert.equal(focusedCount(true), 1);
    assert.equal(focusedCount(false), 0);
});

test('mobile navigation moves focus into the drawer and restores it on close', () => {
    let activeElement;
    const menuToggle = {
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        focus() { activeElement = this; }
    };
    const navLink = {focus() { activeElement = this; }};
    const sidebar = {
        querySelector(selector) {
            assert.equal(selector, '.side-nav a[href]:not([hidden])');
            return navLink;
        },
        contains(element) { return element === navLink; }
    };
    const backdrop = {};
    const bodyClasses = new Set();
    const document = {
        documentElement: mockElement(),
        body: {classList: {
            contains: name => bodyClasses.has(name),
            toggle(name, force) {
                if (force) bodyClasses.add(name);
                else bodyClasses.delete(name);
            }
        }},
        activeElement: null,
        addEventListener() {},
        getElementById(id) {
            return {'menu-toggle': menuToggle, sidebar, 'sidebar-backdrop': backdrop}[id] || null;
        }
    };
    Object.defineProperty(document, 'activeElement', {
        get() { return activeElement; },
        set(value) { activeElement = value; }
    });
    const context = vm.createContext({
        window: {
            matchMedia(query) {
                return {matches: query === '(max-width: 900px)', addEventListener() {}};
            }
        },
        document, localStorage: {getItem: () => null, setItem() {}}, console
    });
    vm.runInContext(themeScript, context);

    context.toggleSidebar(true);
    assert.equal(activeElement, navLink);
    assert.equal(menuToggle.attributes['aria-expanded'], 'true');
    assert.equal(backdrop.hidden, false);

    context.toggleSidebar(false);
    assert.equal(activeElement, menuToggle);
    assert.equal(menuToggle.attributes['aria-expanded'], 'false');
    assert.equal(backdrop.hidden, true);
});

test('workspace header removes repeated branding and keeps account actions together', () => {
    const base = fs.readFileSync(path.join(root, 'src', 'templates', 'base.html'), 'utf8');
    assert.match(base, /\('More', \[\('settings', '\/settings', 'Settings'\)\]\)/);
    assert.match(base, /id="nav-billing"[\s\S]*?id="nav-user-management"/);
    assert.match(base, /<details class="account-menu" id="account-menu">[\s\S]*?Change password[\s\S]*?Sign out[\s\S]*?<\/details>/);
    assert.doesNotMatch(base, /AsuraTECH|id="account-plan-status"|id="sidebar-logout"/);
    assert.match(base, /class="brand-logo"[^>]+diskubre-logo\.png/);
    assert.match(base, />diskubre<small>LEAD WORKSPACE<\/small>/);
});

test('workspace data actions dispatch typed values and keyboard events', () => {
    const handlers = {};
    const calls = [];
    const actionElement = {
        dataset: {
            clickAction: 'captureWorkspaceAction',
            clickArgument: '-1',
            clickArgumentType: 'number'
        }
    };
    const document = {
        documentElement: mockElement(),
        body: mockElement(),
        addEventListener(type, handler) { handlers[type] = handler; },
        getElementById() { return null; }
    };
    const context = vm.createContext({
        window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
        document,
        localStorage: { getItem: () => null, setItem() {} },
        captureWorkspaceAction: (...args) => calls.push(args),
        console
    });
    vm.runInContext(themeScript, context);

    handlers.click({
        target: { closest: () => actionElement }
    });
    assert.deepEqual(calls[0], [-1]);

    const keydownElement = {
        dataset: {
            keydownAction: 'captureWorkspaceAction',
            keydownArgument: 'event'
        }
    };
    const keydownEvent = {
        key: 'Enter',
        target: { closest: () => keydownElement },
        preventDefault() {}
    };
    handlers.keydown(keydownEvent);
    assert.equal(calls[1][0], keydownEvent);
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

test('accessible modal traps focus, closes on Escape, and restores prior focus', () => {
    const handlers = {};
    let activeElement;
    const trigger = {
        isConnected: true,
        focus() { activeElement = this; }
    };
    const classes = new Set(['hidden']);
    const attributes = {};
    const modal = {
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); },
            contains(value) { return classes.has(value); }
        },
        setAttribute(name, value) { attributes[name] = value; },
        getAttribute(name) { return attributes[name] ?? null; },
        focus() { activeElement = this; },
        contains(node) { return node === this || focusable.includes(node); },
        querySelectorAll() { return focusable; }
    };
    function makeFocusable() {
        return {
            disabled: false,
            focus() { activeElement = this; },
            getAttribute() { return null; },
            closest() { return null; }
        };
    }
    const focusable = [makeFocusable(), makeFocusable()];
    const document = {
        activeElement: null,
        documentElement: mockElement(),
        body: mockElement(),
        addEventListener(type, handler) { handlers[type] = handler; },
        getElementById(id) { return id === 'test-modal' ? modal : null; }
    };
    Object.defineProperty(document, 'activeElement', {
        get() { return activeElement; },
        set(value) { activeElement = value; }
    });
    activeElement = trigger;
    const context = vm.createContext({
        window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
        document, localStorage: { getItem: () => null, setItem() {} }, console
    });
    vm.runInContext(themeScript, context);

    context.openAccessibleModal('test-modal', { onEscape: () => context.closeAccessibleModal('test-modal') });
    assert.equal(classes.has('hidden'), false);
    assert.equal(attributes.role, 'dialog');
    assert.equal(attributes['aria-modal'], 'true');
    assert.equal(activeElement, focusable[0]);

    activeElement = trigger;
    let prevented = false;
    handlers.keydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(activeElement, focusable[0]);

    activeElement = focusable[0];
    handlers.keydown({ key: 'Tab', shiftKey: true, preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(activeElement, focusable[1]);

    activeElement = focusable[1];
    handlers.keydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented = true; } });
    assert.equal(activeElement, focusable[0]);

    handlers.keydown({ key: 'Escape', preventDefault() { prevented = true; } });
    assert.equal(classes.has('hidden'), true);
    assert.equal(activeElement, trigger);
});

test('non-dismissible progress modal retains focus and still traps Tab', () => {
    const handlers = {};
    let activeElement;
    const classes = new Set(['hidden']);
    const focusable = [{
        disabled: false,
        focus() { activeElement = this; },
        getAttribute() { return null; },
        closest() { return null; }
    }];
    const modal = {
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); }
        },
        setAttribute() {},
        focus() { activeElement = this; },
        contains(node) { return node === this || focusable.includes(node); },
        querySelectorAll() { return focusable; }
    };
    const document = {
        documentElement: mockElement(), body: mockElement(),
        addEventListener(type, handler) { handlers[type] = handler; },
        getElementById: () => modal,
        activeElement: null
    };
    const context = vm.createContext({
        window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
        document, localStorage: { getItem: () => null, setItem() {} }, console
    });
    vm.runInContext(themeScript, context);
    context.openAccessibleModal('progress-modal', { closeOnEscape: false });

    let prevented = false;
    handlers.keydown({ key: 'Escape', preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(classes.has('hidden'), false);
    handlers.keydown({ key: 'Tab', preventDefault() { prevented = true; } });
    assert.equal(classes.has('hidden'), false);
});

test('workspace modal markup exposes dialog names and labeled close buttons', () => {
    for (const [id, label] of [
        ['loading-overlay', 'discovery-progress-title'],
        ['job-profile-modal', 'job-modal-company'],
        ['profile-modal', 'modal-profile-title'],
        ['screenshot-lightbox', 'screenshot-lightbox-title']
    ]) {
        const modal = template.match(new RegExp(`<div id="${id}"[\\s\\S]*?>`))?.[0];
        assert.ok(modal, `Missing ${id}`);
        assert.match(modal, /role="dialog"/);
        assert.match(modal, /aria-modal="true"/);
        assert.match(modal, new RegExp(`aria-labelledby="${label}"`));
        assert.match(modal, /tabindex="-1"/);
    }
    assert.match(template, /aria-label="Close job profile"/);
    assert.match(template, /aria-label="Close prospect profile"/);
    assert.match(template, /aria-label="Close expanded screenshot"/);
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
        document, window: { addEventListener() {} }, URLSearchParams, console: { error() {} },
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
    const context = vm.createContext({ document, window: { addEventListener() {} }, URLSearchParams, console });
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

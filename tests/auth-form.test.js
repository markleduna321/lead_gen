const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'auth.js'), 'utf8');

function setup() {
    const listeners = {};
    const elements = new Map();
    const password = {
        name: 'password', value: '', attributes: {},
        getAttribute(name) { return this.attributes[name] || null; },
        setAttribute(name, value) { this.attributes[name] = value; },
        removeAttribute(name) { delete this.attributes[name]; },
        hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
    };
    const meter = {value: 0, dataset: {}};
    const strength = {textContent: ''};
    const form = {
        addEventListener(type, handler) { listeners[type] = handler; }
    };
    elements.set('password', password);
    elements.set('password-strength-meter', meter);
    elements.set('password-strength', strength);
    const context = vm.createContext({
        document: {
            querySelector: () => form,
            getElementById: id => elements.get(id) || null
        }
    });
    vm.runInContext(source, context);
    return {listeners, elements, password, meter, strength};
}

test('password strength feedback rejects common and low-entropy values, accepts strong passwords', () => {
    const ui = setup();
    for (const [value, score, label] of [
        ['password123', 1, 'Weak'],
        ['Password123!', 1, 'Weak'],
        ['aaaaaaaaaa', 1, 'Weak'],
        ['abcdefghij', 1, 'Weak'],
        ['correct-horse-battery', 3, 'Good'],
        ['LocalPreviewOnly123!', 4, 'Strong']
    ]) {
        ui.password.value = value;
        ui.listeners.input({target: ui.password});
        assert.equal(ui.meter.value, score);
        assert.equal(ui.strength.textContent, `Password strength: ${label}`);
    }
});

test('editing a field clears its stale server error and invalid state', () => {
    const ui = setup();
    const error = {removed: false, remove() { this.removed = true; }};
    ui.elements.set('password-error', error);
    ui.password.attributes['aria-invalid'] = 'true';
    ui.password.attributes['aria-describedby'] = 'password-hint password-strength password-error';

    ui.password.value = 'LocalPreviewOnly123!';
    ui.listeners.input({target: ui.password});

    assert.equal(error.removed, true);
    assert.equal(ui.password.hasAttribute('aria-invalid'), false);
    assert.equal(ui.password.getAttribute('aria-describedby'), 'password-hint password-strength');
});

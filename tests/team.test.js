const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'team.js'), 'utf8');

function setup({storage = new Map(), token = '', signedIn = true, fail = false} = {}) {
    const notice = {hidden: true, textContent: ''};
    const accept = {
        disabled: false, hidden: false, dataset: {}, listeners: {},
        addEventListener(event, handler) { this.listeners[event] = handler; }
    };
    const calls = [];
    let refreshes = 0;
    const window = {
        location: {hash: token ? `#${token}` : '', pathname: '/team-invitation'},
        refreshAccountPlan: async () => { refreshes++; }
    };
    vm.runInNewContext(source, {
        window,
        history: {replaceState() { window.location.hash = ''; }},
        sessionStorage: {
            getItem: key => storage.get(key),
            setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key)
        },
        document: {
            body: {dataset: {page: 'team-invitation'}},
            getElementById(id) {
                if (id === 'team-message') return notice;
                if (id === 'accept-team-invitation' && signedIn) return accept;
                return null;
            },
            querySelector: () => ({content: 'test-csrf'}),
            querySelectorAll: selector => selector === 'button, input' && signedIn ? [accept] : []
        },
        async fetch(url, options) {
            calls.push({url, options});
            return {ok: !fail, json: async () => fail
                ? {status: 'error', message: 'Invitation expired'}
                : {status: 'success'}};
        }
    });
    return {storage, notice, accept, calls, window, refreshes: () => refreshes};
}

test('Invitation fragment survives sign-in and acceptance refreshes the shared wallet display', async () => {
    const anonymous = setup({token: 'private-email-token', signedIn: false});
    assert.equal(anonymous.window.location.hash, '');
    assert.equal(anonymous.storage.get('team-invitation'), 'private-email-token');
    const member = setup({storage: anonymous.storage});
    assert.equal(member.accept.disabled, false);
    await member.accept.listeners.click();
    assert.equal(member.calls[0].url, '/api/team/accept');
    assert.equal(member.calls[0].options.headers['X-CSRF-Token'], 'test-csrf');
    assert.equal(JSON.parse(member.calls[0].options.body).token, 'private-email-token');
    assert.equal(member.storage.size, 0);
    assert.equal(member.accept.hidden, true);
    assert.equal(member.refreshes(), 1);
    assert.match(member.notice.textContent, /shared owner balance; your leads remain private/);
});

test('Failed acceptance retains the token and restores controls without claiming success', async () => {
    const member = setup({token: 'private-email-token', fail: true});
    await member.accept.listeners.click();
    assert.equal(member.accept.disabled, false);
    assert.equal(member.accept.hidden, false);
    assert.equal(member.storage.get('team-invitation'), 'private-email-token');
    assert.match(member.notice.textContent, /Invitation expired/);
    assert.equal(member.notice.className, 'settings-error');
    assert.equal(member.refreshes(), 0);
    assert.equal(setup().accept.disabled, true);
});

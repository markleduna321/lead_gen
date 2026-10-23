const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'templates', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(source => source.includes('function submitActiveDiscovery'))
    .replace('{{ carto_basemap_api_key | tojson }}', '""');

function setup() {
    const elements = new Map();
    const checked = { 'biz-type': ['Cafe'], 'scan-source': ['google'], 'job-service': ['video_editing'] };
    const requests = [];
    const timers = new Map();
    let timerId = 0;
    function element(id) {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, {
                value: '', innerText: '', attributes: {}, children: [],
                classList: {
                    add: value => classes.add(value),
                    remove: value => classes.delete(value),
                    contains: value => classes.has(value),
                    toggle(value, force) { if (force) classes.add(value); else classes.delete(value); }
                },
                setAttribute(name, value) { this.attributes[name] = value; },
                replaceChildren(...children) { this.children = children; },
                appendChild(child) { this.children.push(child); },
                focus() { this.focused = true; }
            });
        }
        return elements.get(id);
    }
    const context = vm.createContext({
        document: {
            body: { dataset: { page: 'prospects' } },
            getElementById: element,
            createElement() { return {}; },
            querySelectorAll(selector) {
                const name = selector.match(/name="([^"]+)"/)[1];
                return checked[name].map(value => ({ value }));
            }
        },
        window: { location: { assign() {} } }, URLSearchParams, AbortSignal, console: { error() {} },
        setTimeout(handler) { const id = ++timerId; timers.set(id, handler); return id; },
        clearTimeout(id) { timers.delete(id); }, clearInterval() {},
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, json: async () => ({ status: 'success', run_id: 'qa-run' }) };
        }
    });
    vm.runInContext(script, context);
    vm.runInContext(`
        setGlobalLock = value => { isWorking = value; };
        setQueueView = view => { globalThis.resultView = view; };
        startLiveConsoleLogStreamingLoop = async () => {};
        startLocalDiscoveryPolling = async runId => { globalThis.trackedRunId = runId; };
    `, context);
    element('modal-location').value = 'Town';
    element('modal-rating-min').value = '4';
    element('modal-reviews-max').value = '100';
    element('modal-radius').value = '10';
    context.applyLocation({ label: 'Town', lat: 10.6765, lng: 122.9509 });
    return { context, element, checked, requests, timers };
}

test('one discovery workspace with two search modes', () => {
    assert.match(html, /id="discovery-modal"/);
    assert.doesNotMatch(html, /id="btn-job-discover"|id="job-discovery-modal"/);
    assert.equal((html.match(/function toggleDiscoveryModal\(/g) || []).length, 1);
});

test('unavailable LinkedIn discovery cannot be selected', () => {
    assert.match(html, /name="scan-source" value="linkedin" disabled/);
    assert.match(html, /LinkedIn \(Not available\)/);
    assert.match(html, /LinkedIn discovery is not integrated yet/);
});

test('prospect details distinguish business websites from social profiles', () => {
    const { context, element } = setup();
    vm.runInContext(`
        currentLeads = [{ id: 1, business_name: 'QA', category: 'Dental',
            website_url: 'https://example.test', profile_url: 'https://instagram.com/qa',
            email: 'qa@example.test' }];
        selectedLeadId = 1;
    `, context);
    context.toggleProfileModal(true);
    assert.equal(element('profile-website-link').href, 'https://example.test');
    assert.equal(element('profile-social-link').href, 'https://instagram.com/qa');
    assert.equal(element('profile-email-input').value, 'qa@example.test');
    assert.equal(element('profile-rating').innerText, 'Not available');
    assert.equal(element('profile-reviews').innerText, 'Not available');
    vm.runInContext("currentLeads[0].website_url = null; currentLeads[0].profile_url = 'javascript:alert(1)';", context);
    context.toggleProfileModal(true);
    assert.equal(element('profile-website-link').innerText, 'Not found');
    assert.equal(element('profile-social-link').href, '#');
    assert.equal(element('profile-social-link').tabIndex, -1);
    vm.runInContext("currentLeads[0].source_platform = 'instagram'; currentLeads[0].rating = 0; currentLeads[0].review_count = 0;", context);
    context.toggleProfileModal(true);
    assert.equal(element('profile-rating').innerText, 'Not available');
});

test('tabs preserve form values and support keyboard navigation', () => {
    const { context, element } = setup();
    element('job-country-input').value = 'Canada';
    context.setDiscoveryMode('jobs');
    assert.equal(element('discovery-tab-jobs').attributes['aria-selected'], 'true');
    assert.equal(element('btn-discovery-submit').innerText, 'Find Hiring Companies');
    let prevented = false;
    context.handleDiscoveryTabKey({ key: 'Home', preventDefault() { prevented = true; } });
    assert.ok(prevented);
    assert.ok(element('discovery-tab-local').focused);
    assert.equal(element('modal-location').value, 'Town');
    assert.equal(element('job-country-input').value, 'Canada');
});

test('local discovery tracks its own run without clearing shared logs', async () => {
    const { context, requests } = setup();
    await context.submitActiveDiscovery();
    assert.equal(requests[0].url, '/api/trigger/discover');
    assert.equal(requests.length, 1);
    assert.equal(context.trackedRunId, 'qa-run');
    const payload = JSON.parse(requests[0].options.body);
    assert.deepEqual(payload.categories, ['Cafe']);
    assert.equal(payload.radius_km, 10);
    assert.equal(payload.rating_min, 4);
    assert.equal(payload.max_reviews, 100);
    assert.equal(context.resultView, 'prospects');
});

test('unlimited reviews is sent as null and filter scope is explained', async () => {
    const { context, element, requests } = setup();
    element('modal-reviews-max').value = '';
    await context.submitDiscoveryRequest();
    assert.equal(JSON.parse(requests[0].options.body).max_reviews, null);
    assert.match(html, /Stars and review limits apply only to Google Maps and Yelp/);
    assert.match(html, /Facebook and Instagram leads are included without these filters/);
    assert.match(html, /25 or fewer/);
    assert.doesNotMatch(html, /value="99999"|Under 25 Reviews/);
});

test('hiring discovery uses only job fields and separate results', async () => {
    const { context, element, requests } = setup();
    context.setDiscoveryMode('jobs');
    element('job-country-input').value = 'Canada';
    await context.submitActiveDiscovery();
    assert.equal(requests[1].url, '/api/trigger/discover-jobs');
    assert.deepEqual(JSON.parse(requests[1].options.body), {
        service_types: ['video_editing'], country: 'Canada'
    });
    assert.equal(context.resultView, 'jobs');
});

test('validation prevents empty searches', async () => {
    const { context, element, checked, requests } = setup();
    element('modal-location').value = '';
    await context.submitActiveDiscovery();
    assert.match(element('discovery-error').innerText, /location/);
    context.setDiscoveryMode('jobs');
    checked['job-service'] = [];
    await context.submitActiveDiscovery();
    assert.match(element('discovery-error').innerText, /service offering/);
    assert.equal(requests.length, 0);
});

test('request errors restore the dialog and release the UI lock', async () => {
    const { context, element } = setup();
    context.fetch = async () => ({ ok: false, json: async () => ({ message: 'Search unavailable' }) });
    await context.submitActiveDiscovery();
    assert.match(element('discovery-error').innerText, /Search unavailable/);
    assert.equal(element('discovery-modal').classList.contains('hidden'), true);
    assert.equal(element('loading-overlay').classList.contains('hidden'), true);
    assert.equal(vm.runInContext('isWorking', context), false);
});

test('trigger rejection displays the backend error instead of waiting forever', async () => {
    const { context, element } = setup();
    context.fetch = async url => url.endsWith('/clear')
        ? { ok: true }
        : { ok: false, json: async () => ({ status: 'error', message: 'Search unavailable' }) };
    await context.submitActiveDiscovery();
    assert.equal(element('discovery-error').innerText, 'Search unavailable');
    assert.equal(vm.runInContext('isWorking', context), false);
    assert.equal(context.resultView, undefined);
});

function locationResponse(label = 'Austin', lat = 30.2672, lng = -97.7431) {
    return {
        ok: true,
        json: async () => ({
            features: [{ properties: { name: label, country: 'United States' },
                geometry: { coordinates: [lng, lat] } }]
        })
    };
}

test('edited location is resolved before discovery, never using the stale pin', async () => {
    const { context, element, requests } = setup();
    const originalFetch = context.fetch;
    element('modal-location').value = 'Austin';
    context.fetch = async (url, options) => {
        if (url.startsWith('https://photon.komoot.io/api/')) {
            requests.push({ url });
            return locationResponse();
        }
        return originalFetch(url, options);
    };
    await context.submitDiscoveryRequest();
    assert.match(requests[0].url, /q=Austin/);
    const payload = JSON.parse(requests[1].options.body);
    assert.equal(payload.location, 'Austin, United States');
    assert.equal(payload.lat, 30.2672);
    assert.equal(payload.lng, -97.7431);
});

test('lookup errors and no matches prevent log clearing and paid discovery', async () => {
    for (const response of [
        { ok: false },
        { ok: true, json: async () => ({ features: [] }) },
        { ok: true, json: async () => ({ features: [{}] }) }
    ]) {
        const { context, element, requests } = setup();
        element('modal-location').value = 'Unknown';
        context.fetch = async () => response;
        await context.submitDiscoveryRequest();
        assert.equal(requests.length, 0);
        assert.ok(element('discovery-error').innerText);
        assert.equal(vm.runInContext('locationResolving', context), false);
    }
});

test('suggestions debounce, cache lookups, and selecting one updates the pin', async () => {
    const { context, element, timers } = setup();
    let lookups = 0;
    context.fetch = async () => { lookups++; return locationResponse(); };
    element('modal-location').value = 'Au';
    context.handleLocationInput();
    assert.equal(timers.size, 0);
    element('modal-location').value = 'Aus';
    context.handleLocationInput();
    element('modal-location').value = 'Austin';
    context.handleLocationInput();
    assert.equal(timers.size, 1);
    await [...timers.values()][0]();
    assert.equal(element('location-suggestions').children[0].value, 'Austin, United States');
    await context.searchLocations('Austin');
    assert.equal(lookups, 1);
    element('modal-location').value = 'Austin, United States';
    context.handleLocationInput();
    assert.equal(vm.runInContext('confirmedLocation', context), 'Austin, United States');
    assert.equal(vm.runInContext('activeCoordinates.lat', context), 30.2672);
    assert.equal(timers.size, 0);
});

test('stale suggestions cannot overwrite a newer query', async () => {
    const { context, element, timers } = setup();
    let finish;
    context.fetch = () => new Promise(resolve => { finish = resolve; });
    element('modal-location').value = 'Austin';
    context.handleLocationInput();
    const pending = [...timers.values()][0]();
    element('modal-location').value = 'Toronto';
    context.handleLocationInput();
    finish(locationResponse());
    await pending;
    assert.equal(element('location-suggestions').children.length, 0);
});

test('location edits during submission block discovery and duplicate submissions', async () => {
    const { context, element, requests } = setup();
    let finish;
    let lookups = 0;
    context.fetch = () => { lookups++; return new Promise(resolve => { finish = resolve; }); };
    element('modal-location').value = 'Austin';
    const pending = context.submitDiscoveryRequest();
    await context.submitDiscoveryRequest();
    assert.equal(lookups, 1);
    element('modal-location').value = 'Toronto';
    context.handleLocationInput();
    finish(locationResponse());
    await pending;
    assert.equal(requests.length, 0);
    assert.match(element('discovery-error').innerText, /changed/);
});

test('manual pins preserve exact coordinates even if the address lookup fails', async () => {
    const { context, element, requests } = setup();
    context.fetch = async () => { throw new Error('Offline'); };
    await context.setDraggedLocation(0, 30);
    assert.equal(element('modal-location').value, '0.00000, 30.00000');
    assert.match(element('location-status').innerText, /preserved/);
    const fetchDiscovery = setup().context.fetch;
    context.fetch = async (url, options) => {
        requests.push({ url, options });
        return fetchDiscovery(url, options);
    };
    await context.submitDiscoveryRequest();
    assert.equal(requests[0].url, '/api/trigger/discover');
    const payload = JSON.parse(requests[0].options.body);
    assert.equal(payload.lat, 0);
    assert.equal(payload.lng, 30);
});

test('a delayed reverse lookup cannot overwrite an edited location', async () => {
    const { context, element } = setup();
    let finish;
    context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = context.setDraggedLocation(1, 2);
    element('modal-location').value = 'Toronto';
    context.handleLocationInput();
    finish(locationResponse());
    await pending;
    assert.equal(element('modal-location').value, 'Toronto');
});

test('reverse address labels never snap a dragged pin to the returned feature center', async () => {
    const { context, element } = setup();
    context.fetch = async () => locationResponse();
    await context.setDraggedLocation(30.3, -97.8);
    assert.equal(element('modal-location').value, 'Austin, United States');
    assert.equal(vm.runInContext('activeCoordinates.lat', context), 30.3);
    assert.equal(vm.runInContext('activeCoordinates.lng', context), -97.8);
});

test('network timeouts block discovery and suggestion failures remain visible', async () => {
    const { context, element, timers, requests } = setup();
    context.fetch = async () => { throw new Error('Location request timed out.'); };
    element('modal-location').value = 'Austin';
    context.handleLocationInput();
    await [...timers.values()][0]();
    assert.match(element('location-status').innerText, /unavailable/);
    await context.submitDiscoveryRequest();
    assert.match(element('discovery-error').innerText, /timed out/);
    assert.equal(requests.length, 0);
    assert.equal(vm.runInContext('locationResolving', context), false);
});

function pollingSetup(run) {
    const fixture = setup();
    vm.runInContext("activeLocalRunId = 'qa-run'; isWorking = true; loadQueue = async () => { globalThis.queueRefreshed = true; };", fixture.context);
    fixture.context.fetch = async () => ({ ok: true, json: async () => ({ status: 'success', run }) });
    return fixture;
}

test('local polling ignores completion-looking log text while status is running', async () => {
    const { context, timers } = pollingSetup({
        state: 'running', total: 2, completed: 1, errors: [], logs: ['Complete process successfully cleared background pass.']
    });
    await context.pollLocalDiscoveryRun('qa-run');
    assert.equal(vm.runInContext('isWorking', context), true);
    assert.equal(timers.size, 1);
});

test('failed runs unlock without success refresh and show partial-save warning', async () => {
    const { context, element } = pollingSetup({
        state: 'failed', total: 2, completed: 2, errors: ['Yelp / Cafe: quota exceeded'], logs: []
    });
    await context.pollLocalDiscoveryRun('qa-run');
    assert.match(element('discovery-error').innerText, /Some leads may have been saved.*quota exceeded/);
    assert.equal(vm.runInContext('isWorking', context), false);
    assert.equal(element('loading-overlay').classList.contains('hidden'), true);
    assert.equal(context.queueRefreshed, undefined);
});

test('successful runs refresh only after structured success', async () => {
    const { context } = pollingSetup({ state: 'succeeded', total: 1, completed: 1, errors: [], logs: [] });
    await context.pollLocalDiscoveryRun('qa-run');
    assert.equal(context.queueRefreshed, true);
    assert.equal(vm.runInContext('activeLocalRunId', context), null);
});

test('missing run status and repeated network failures release the UI with warnings', async () => {
    for (const missing of [true, false]) {
        const { context, element } = pollingSetup({});
        context.fetch = async () => {
            if (missing) return { ok: false, status: 404, json: async () => ({ status: 'error', message: 'The app may have restarted.' }) };
            throw new Error('Offline');
        };
        await context.pollLocalDiscoveryRun('qa-run', missing ? 0 : 2);
        assert.equal(vm.runInContext('isWorking', context), false);
        assert.match(element('discovery-error').innerText, missing ? /restarted/ : /worker may still be running/);
    }
});

test('stop waiting does not claim cancellation and stale status cannot update the UI', async () => {
    const { context, element } = pollingSetup({});
    let finish;
    context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = context.pollLocalDiscoveryRun('qa-run');
    context.stopWaitingForLocalDiscovery();
    finish({ ok: true, json: async () => ({ status: 'success', run: { state: 'succeeded', errors: [], logs: [] } }) });
    await pending;
    assert.match(element('discovery-error').innerText, /worker may still be running/);
    assert.equal(context.queueRefreshed, undefined);
});

test('a missing tracking ID is not treated as a monitorable successful search', async () => {
    const { context, element } = setup();
    context.fetch = async () => ({ ok: true, json: async () => ({ status: 'success' }) });
    await context.submitDiscoveryRequest();
    assert.match(element('discovery-error').innerText, /without a tracking ID/);
    assert.equal(vm.runInContext('isWorking', context), false);
    assert.equal(context.resultView, undefined);
});

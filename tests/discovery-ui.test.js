const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'templates', 'dashboard.html'), 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(source => source.includes('function submitActiveDiscovery'))
    .replace('{{ carto_basemap_api_key | tojson }}', '""')
    .replace('{{ hiring_builtin_services | tojson }}', JSON.stringify([
        ['video_editing', 'Video Editing'], ['bpo_customer_service', 'Customer Service BPO'],
        ['bpo_data_entry', 'Data Entry BPO'], ['social_media_management', 'Social Media Management'],
        ['virtual_assistant', 'Virtual Assistant']
    ].map(([id, name]) => ({ id, name, builtin: true, roles: Array(4).fill('QA role'), title_phrases: [] }))));

function setup(page = 'prospects') {
    const elements = new Map();
    const checked = { 'biz-type': ['Cafe'], 'scan-source': ['google'], 'job-service': ['video_editing'] };
    const requests = [];
    const timers = new Map();
    let timerId = 0;
    function element(id) {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, {
                value: '', innerText: '', textContent: '', dataset: {}, attributes: {}, children: [],
                classList: {
                    add: value => classes.add(value),
                    remove: value => classes.delete(value),
                    contains: value => classes.has(value),
                    toggle(value, force) { if (force) classes.add(value); else classes.delete(value); }
                },
                setAttribute(name, value) { this.attributes[name] = value; },
                removeAttribute(name) { delete this.attributes[name]; if (name === 'value') this.value = ''; },
                replaceChildren(...children) { this.children = children; },
                appendChild(child) { child.parent = this; this.children.push(child); },
                querySelectorAll() {
                    return this.children.filter(child => child.attributes?.['data-custom-service']);
                },
                focus() { this.focused = true; }
            });
        }
        return elements.get(id);
    }
    const context = vm.createContext({
        document: {
            body: { dataset: { page } },
            getElementById: element,
            createElement() {
                return {
                    children: [], attributes: {},
                    appendChild(child) { child.parent = this; this.children.push(child); },
                    setAttribute(name, value) { this.attributes[name] = value; },
                    remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
                };
            },
            querySelectorAll(selector) {
                const name = selector.match(/name="([^"]+)"/)[1];
                const preset = checked[name].map(value => ({ value }));
                if (name === 'job-service') {
                    const custom = element('custom-hiring-services').children
                        .map(label => label.children[0]).filter(input => input.checked);
                    return [...preset, ...custom];
                }
                return preset;
            }
        },
        window: { location: { assign() {} } }, URL, URLSearchParams, AbortSignal, console: { error() {} },
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
    element('job-pages-input').value = '1';
    context.applyLocation({ label: 'Town', lat: 10.6765, lng: 122.9509 });
    return { context, element, checked, requests, timers };
}

test('one discovery workspace with two search modes', () => {
    assert.match(html, /id="discovery-modal"/);
    assert.doesNotMatch(html, /id="btn-job-discover"|id="job-discovery-modal"/);
    assert.equal((html.match(/function toggleDiscoveryModal\(/g) || []).length, 1);
});

test('Prospects removes bulk pitch drafting while retaining individual drafting and other tools', () => {
    assert.doesNotMatch(html, /btn-bulk-pitch|bulk-pitch-modal|bulk-pitch-limit|submitBulkPitchRequest|pitch-bulk/);
    assert.match(html, /id="btn-individual-ai"/);
    assert.match(html, /id="btn-score-leads"/);
    assert.match(html, /id="btn-followup-sweep"/);
    assert.match(html, /href="\/api\/export"/);
});

test('Locked AI drafts show an upgrade toast built without HTML injection', () => {
    const source = html.match(/function showUpgradeToast\(message\) \{[\s\S]*?\n        \}/)[0];
    const created = [];
    const body = { children: [], appendChild(node) { this.children.push(node); } };
    const document = {
        body,
        createElement(tag) {
            const node = { tag, attributes: {}, children: [], textContent: '',
                setAttribute(name, value) { this.attributes[name] = value; },
                append(...nodes) { this.children.push(...nodes); }, remove() {} };
            created.push(node);
            return node;
        },
    };
    const context = { document, setTimeout() {} };
    vm.runInNewContext(`${source}; showUpgradeToast('<img src=x onerror=alert(1)> Upgrade please');`, context);
    const [toast] = body.children;
    const [text, link] = toast.children;
    assert.equal(text.textContent, '<img src=x onerror=alert(1)> Upgrade please');
    assert.equal(link.tag, 'a');
    assert.equal(link.href, '/billing');
    assert.equal(link.textContent, 'Upgrade');
    assert.ok(created.every(node => !('innerHTML' in node)));
    assert.match(html, /data\.code === 'pitch_locked'\) \{\s*showUpgradeToast\(/);
});

test('Maintenance job buttons stay disabled while the server reports the job running', async () => {
    const { context, element, timers } = setup();
    const button = element('btn-score-leads');
    button.textContent = '  Score leads  ';
    const toasts = [];
    const calls = [];
    let running = true;
    let reloads = 0;
    context.showToast = message => toasts.push(message);
    context.loadQueue = () => { reloads += 1; };
    context.fetch = async (url, options) => {
        calls.push({ url, method: options?.method || 'GET' });
        if (url === '/api/trigger/score-leads') {
            return { ok: true, json: async () => ({ status: 'success', message: 'Lead scoring started.',
                jobs: { 'score-leads': { running: true } } }) };
        }
        return { ok: true, json: async () => ({ status: 'success', jobs: { 'score-leads': { running } } }) };
    };

    await context.triggerMaintenanceJob('score-leads');
    assert.deepEqual(toasts, ['Lead scoring started.']);
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, 'Scoring leads…');
    assert.equal(button.attributes['aria-busy'], 'true');
    await context.triggerMaintenanceJob('score-leads');
    assert.equal(calls.length, 1);

    const poll = () => { const [id, handler] = [...timers].pop(); timers.delete(id); return handler(); };
    await poll();
    assert.equal(button.disabled, true);
    assert.equal(reloads, 0);
    running = false;
    await poll();
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, 'Score leads');
    assert.equal(button.attributes['aria-busy'], 'false');
    assert.equal(reloads, 1);
    assert.deepEqual(calls.map(call => call.url), ['/api/trigger/score-leads', '/api/jobs/status', '/api/jobs/status']);
});

test('Rejected maintenance jobs show the server reason and re-enable the button', async () => {
    const { context, element } = setup();
    const button = element('btn-followup-sweep');
    button.textContent = 'Check follow-ups';
    const toasts = [];
    context.showToast = message => toasts.push(message);
    context.fetch = async () => ({ ok: false, json: async () => ({ status: 'error', code: 'job_cooldown',
        message: 'Follow-up check can run again in 5 minutes.', jobs: { 'followup-sweep': { running: false } } }) });
    await context.triggerMaintenanceJob('followup-sweep');
    assert.deepEqual(toasts, ['Follow-up check can run again in 5 minutes.']);
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, 'Check follow-ups');
});

test('completed searches preserve credit settlement across the results redirect', () => {
    const { context, element } = setup();
    const stored = new Map();
    context.sessionStorage = {
        setItem(key, value) { stored.set(key, value); },
        getItem(key) { return stored.get(key); },
        removeItem(key) { stored.delete(key); }
    };
    context.rememberDiscoveryOutcome(8, [], ' New leads: 0. Search credits charged: 3.00; refunded: 7.00.');
    context.showDiscoveryOutcome();
    assert.equal(element('queue-notice').innerText,
        'Search finished. 8 leads saved. New leads: 0. Search credits charged: 3.00; refunded: 7.00.');
    assert.equal(stored.size, 0);
});

test('hiring guidance explains strict targeting without promising global coverage', () => {
    assert.match(html, /blank means no country filter/);
    assert.match(html, /Strict matching excludes unknown locations/);
    assert.match(html, /Actual job titles must match a selected service/);
    assert.doesNotMatch(html, /leave blank for global/);
});

test('public LinkedIn company search is selectable and clearly scoped', () => {
    assert.match(html, /name="scan-source" value="linkedin"[\s>]/);
    assert.doesNotMatch(html, /name="scan-source" value="linkedin" disabled/);
    assert.match(html, /LinkedIn Companies/);
    assert.match(html, /public LinkedIn company pages indexed by Google via Scrapingdog/);
    assert.match(html, /does not access LinkedIn accounts or private profiles/);
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

test('job profile displays full details, all links and honest contact provenance', () => {
    const { context, element } = setup('jobs');
    vm.runInContext(`
        currentLeads = [{
            id: 7, company_name: '<b>Example Studios</b>', job_title: 'Senior Video Editor',
            job_role_signal: 'video content creator', job_description: 'Full description '.repeat(100),
            website_url: 'https://example.test', email: 'hello@example.test',
            job_apply_link: 'https://example.test/jobs',
            job_details: {
                posted_at: '3 days ago', provider_job_id: 'provider-7', collected_at: '2026-10-23T00:00:00+00:00',
                detected_extensions: { health_insurance: true, schedule_type: 'Full-time' },
                job_highlights: [{ title: 'Benefits', items: ['Health insurance'] }],
                apply_options: [
                    { title: 'Company', link: 'https://example.test/jobs' },
                    { title: '<img onerror=alert(1)>', link: 'https://board.test/job/7' },
                    { title: 'Bad', link: 'javascript:alert(1)' }
                ]
            },
            contact_evidence: {
                status: 'matched', email_status: 'public_domain_match',
                website_source_url: 'https://example.test', checked_at: '2026-10-23',
                email_source_url: 'https://example.test/contact',
                emails: [{ email: 'hello@example.test', source_url: 'https://example.test/contact' }],
                phones: [{ phone: '+1-555-0100', source_url: 'https://example.test/contact' }]
            }
        }];
        selectedLeadId = 7;
    `, context);
    context.toggleJobProfileModal(true);
    assert.equal(element('job-modal-title').innerText, 'Senior Video Editor');
    assert.match(element('job-modal-signal').innerText, /video content creator/);
    assert.ok(element('job-modal-description').innerText.length > 600);
    assert.match(element('job-modal-metadata').innerText, /3 days ago/);
    assert.match(element('job-modal-metadata').innerText, /health insurance: true/);
    assert.match(element('job-modal-highlights').innerText, /Health insurance/);
    assert.equal(element('job-modal-applications').children.length, 2);
    assert.equal(element('job-modal-applications').children[1].innerText, '<img onerror=alert(1)>');
    assert.match(element('job-modal-contact-status').innerText, /deliverability unverified/);
    assert.match(element('job-modal-contacts').innerText, /Listed phone \(unverified\)/);
    assert.equal(element('job-modal-contact-sources').children.length, 2);
    vm.runInContext(`
        currentLeads[0].email = 'manual@example.test';
        currentLeads[0].contact_evidence.email_status = 'manual';
    `, context);
    context.toggleJobProfileModal(true);
    assert.match(element('job-modal-contact-status').innerText, /manually supplied, unverified/);
});

test('legacy job profiles do not imply verified contacts and clear previous metadata', () => {
    const { context, element } = setup('jobs');
    vm.runInContext(`
        currentLeads = [{ id: 1, job_title: 'Editor', website_url: 'javascript:alert(1)',
            job_apply_link: 'javascript:alert(1)' }];
        selectedLeadId = 1;
    `, context);
    context.toggleJobProfileModal(true);
    assert.equal(element('job-modal-website').href, '#');
    assert.equal(element('job-modal-apply').href, '#');
    assert.equal(element('job-modal-location').innerText, 'Unknown');
    assert.equal(element('job-modal-applications').children.length, 0);
    assert.equal(element('job-modal-contact-sources').children.length, 0);
    assert.equal(element('job-modal-highlights').innerText, '');
    assert.match(element('job-modal-contact-status').innerText, /unconfirmed \/ legacy/);
    assert.match(element('job-modal-metadata').innerText, /Not provided/);
});

test('job queue keeps API metadata through table selection, profile and email update', async () => {
    const { context, element } = setup('jobs');
    const requests = [];
    const lead = {
        id: 7, company_name: '<img src=x onerror=alert(1)>', job_title: 'Senior Video Editor',
        job_role_signal: 'video content creator', location: 'Toronto, Canada', status: 'discovered',
        lead_type: 'video_editing', email: 'info@example.test', website_url: 'https://example.test',
        job_description: 'Full description '.repeat(100),
        job_details: { posted_at: '2 days ago', apply_options: [
            { title: 'Company', link: 'https://example.test/apply' }
        ] },
        contact_evidence: { status: 'matched', website_source_url: 'https://example.test',
            email_status: 'public_domain_match',
            emails: [{ email: 'info@example.test', source_url: 'https://example.test/contact' }] }
    };
    context.fetch = async (url, options) => {
        requests.push({ url, options });
        if (url === '/api/job-leads/7/update-email') {
            lead.email = JSON.parse(options.body).email;
            lead.contact_evidence.email_status = 'manual';
            return { ok: true, json: async () => ({ status: 'success' }) };
        }
        return { ok: true, json: async () => ({
            status: 'success', leads: [lead],
            metrics: { total: 1, pending: 1, sent: 0, with_email: 1 }
        }) };
    };
    vm.runInContext(`
        activeQueueView = 'jobs';
        activeSortBy = 'date_desc';
        searchFilterString = 'Studios';
        activeLeadTypeFilter = 'video_editing';
        activeStatusFilter = 'discovered';
    `, context);
    await context.loadQueue();
    const url = new URL(requests[0].url, 'http://localhost');
    assert.equal(url.pathname, '/api/job-leads');
    assert.equal(url.searchParams.get('q'), 'Studios');
    assert.equal(url.searchParams.get('lead_type'), 'video_editing');
    assert.equal(url.searchParams.get('status'), 'discovered');
    assert.equal(url.searchParams.get('sort_by'), 'date_desc');
    assert.equal(element('stat-total').innerText, 1);
    assert.equal(element('stat-followup').innerText, 1);
    assert.equal(element('btn-page-next').disabled, true);
    const row = element('table-body').children[0];
    assert.equal(row.children[0].textContent, lead.company_name);
    assert.equal(row.children[1].textContent, 'Senior Video Editor');
    row.onclick();
    assert.equal(element('job-modal-title').innerText, 'Senior Video Editor');
    assert.equal(element('job-modal-description').innerText, lead.job_description);
    assert.match(element('job-modal-metadata').innerText, /2 days ago/);
    assert.equal(element('job-modal-applications').children[0].href, 'https://example.test/apply');
    element('job-modal-email-input').value = 'manual@example.test';
    await context.saveJobEmailUpdate();
    assert.equal(requests[1].url, '/api/job-leads/7/update-email');
    assert.equal(requests[1].options.method, 'POST');
    context.toggleJobProfileModal(true);
    assert.equal(element('job-modal-email-input').value, 'manual@example.test');
    assert.match(element('job-modal-contact-status').innerText, /manually supplied, unverified/);
    assert.equal(vm.runInContext('isWorking', context), false);
});

function queueResponse(requests, filterOptions) {
    return async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => ({
            status: 'success', leads: [], filter_options: filterOptions,
            metrics: { total: 0, pending: 0, sent: 0, with_email: 0,
                global_total: 0, global_pending: 0, global_sent: 0, global_followup: 0 }
        }) };
    };
}

const optionTexts = select => select.children.map(option => [option.value, option.innerText]);

test('prospect filters send prospect fields and list only sources, industries and statuses in the data', async () => {
    const { context, element } = setup('prospects');
    const requests = [];
    context.fetch = queueResponse(requests, {
        source: [{ value: 'google_maps', label: 'google_maps', count: 4 }, { value: 'yelp', label: 'yelp', count: 1 }],
        category: [{ value: 'law firm', label: 'Law firm', count: 3 }],
        status: [{ value: 'discovered', label: 'discovered', count: 4 },
                 { value: 'sent_followup_1', label: 'sent_followup_1', count: 1 }]
    });
    vm.runInContext(`
        activeQueueView = 'prospects';
        activeSourceFilter = 'yelp';
        activeLeadTypeFilter = 'dentist';
        activeStatusFilter = 'sent_followup_1';
    `, context);
    await context.loadQueue();
    const url = new URL(requests[0].url, 'http://localhost');
    assert.equal(url.pathname, '/api/queue');
    assert.equal(url.searchParams.get('source'), 'yelp');
    assert.equal(url.searchParams.get('category'), 'dentist');
    assert.equal(url.searchParams.get('status_filter'), 'sent_followup_1');
    assert.equal(url.searchParams.has('lead_type'), false);
    assert.equal(url.searchParams.has('via'), false);
    assert.deepEqual(optionTexts(element('filter-source')), [
        ['', 'All Sources'], ['google_maps', 'Google Maps (4)'], ['yelp', 'Yelp (1)']
    ]);
    // A selected industry with no remaining rows stays selectable instead of vanishing.
    assert.deepEqual(optionTexts(element('filter-lead-type')), [
        ['', 'All Industries'], ['law firm', 'Law firm (3)'], ['dentist', 'dentist (0)']
    ]);
    assert.equal(element('filter-lead-type').value, 'dentist');
    assert.deepEqual(optionTexts(element('filter-status')), [
        ['', 'All Active'], ['discovered', 'Discovered (4)'], ['sent_followup_1', 'Follow-Up Sent (1)']
    ]);
});

test('job lead filters use job boards, saved service names and job statuses', async () => {
    const { context, element } = setup('jobs');
    const requests = [];
    vm.runInContext(`hiringServiceCatalog.set('${customService.id}', ${JSON.stringify(customService)});`, context);
    context.fetch = queueResponse(requests, {
        via: [{ value: 'linkedin', label: 'LinkedIn', count: 2 }],
        service: [{ value: 'video_editing', label: 'video_editing', count: 1 },
                  { value: customService.id, label: customService.id, count: 1 }],
        status: [{ value: 'discovered', label: 'discovered', count: 2 }]
    });
    vm.runInContext(`
        activeQueueView = 'jobs';
        activeSourceFilter = 'linkedin';
        activeLeadTypeFilter = '${customService.id}';
        activeStatusFilter = '';
    `, context);
    await context.loadQueue();
    const url = new URL(requests[0].url, 'http://localhost');
    assert.equal(url.pathname, '/api/job-leads');
    assert.equal(url.searchParams.get('via'), 'linkedin');
    assert.equal(url.searchParams.get('lead_type'), customService.id);
    assert.equal(url.searchParams.get('status'), '');
    assert.equal(url.searchParams.has('source'), false);
    assert.equal(url.searchParams.has('category'), false);
    assert.deepEqual(optionTexts(element('filter-source')), [['', 'All Job Boards'], ['linkedin', 'LinkedIn (2)']]);
    assert.deepEqual(optionTexts(element('filter-lead-type')), [
        ['', 'All Services'], ['video_editing', 'Video Editing (1)'], [customService.id, 'Bookkeeping (1)']
    ]);
    assert.equal(element('filter-lead-type').value, customService.id);
    assert.deepEqual(optionTexts(element('filter-status')), [['', 'All Active'], ['discovered', 'Discovered (2)']]);
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

test('LinkedIn-only discovery uses location text and does not require map geocoding', async () => {
    const { context, checked, requests } = setup();
    checked['scan-source'] = ['linkedin'];
    context.geocodeAddressTarget = () => { throw new Error('LinkedIn search should not geocode'); };
    await context.submitDiscoveryRequest();
    const payload = JSON.parse(requests[0].options.body);
    assert.deepEqual(payload.sources, ['linkedin']);
    assert.equal(payload.location, 'Town');
    assert.equal('lat' in payload, false);
    assert.equal('lng' in payload, false);
    assert.equal('radius_km' in payload, false);
});

test('unlimited reviews is sent as null and filter scope is explained', async () => {
    const { context, element, requests } = setup();
    element('modal-reviews-max').value = '';
    await context.submitDiscoveryRequest();
    assert.equal(JSON.parse(requests[0].options.body).max_reviews, null);
    assert.match(html, /Stars and review limits apply only to Google Maps and Yelp/);
    assert.match(html, /Facebook, Instagram, and LinkedIn leads are included without these filters/);
    assert.match(html, /25 or fewer/);
    assert.doesNotMatch(html, /value="99999"|Under 25 Reviews/);
});

test('hiring discovery uses only job fields and separate results', async () => {
    const { context, element, requests } = setup();
    context.setDiscoveryMode('jobs');
    element('job-country-input').value = 'Canada';
    await context.submitActiveDiscovery();
    assert.equal(requests[0].url, '/api/trigger/discover-jobs');
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        service_types: ['video_editing'], country: 'Canada', pages_per_role: 1
    });

    assert.equal(context.resultView, 'jobs');
    assert.equal(context.trackedRunId, 'qa-run');
    assert.equal(requests.length, 1);
});

test('hiring depth shows a credit budget, preserves selection and sends explicit opt-in', async () => {
    const { context, element, checked, requests } = setup();
    context.setDiscoveryMode('jobs');
    assert.match(element('job-search-budget').innerText, /4 Google Jobs \+ 4 LinkedIn Jobs API requests; 8 total/);
    checked['job-service'] = ['video_editing', 'virtual_assistant'];
    element('job-pages-input').value = '5';
    context.updateHiringSearchBudget();
    assert.match(element('job-search-budget').innerText, /40 Google Jobs \+ 40 LinkedIn Jobs API requests; 80 total/);
    assert.match(element('job-search-budget').innerText, /plus company-contact searches/);
    context.setDiscoveryMode('local');
    context.setDiscoveryMode('jobs');
    assert.equal(element('job-pages-input').value, '5');
    await context.submitActiveDiscovery();
    assert.equal(JSON.parse(requests[0].options.body).pages_per_role, 5);
    assert.match(html, /<option value="1" selected>1 page \(default\)<\/option>/);
    assert.match(html, /More pages can consume paid API credits/);
    assert.match(html, /id="discovery-panel-jobs"[^>]*onchange="updateHiringSearchBudget\(\)"/);
});

test('invalid hiring depth is rejected before any search request', async () => {
    const { context, element, requests } = setup();
    context.setDiscoveryMode('jobs');
    for (const value of ['', '0', '4', '6', '2.5', 'invalid']) {
        element('job-pages-input').value = value;
        await context.submitActiveDiscovery();
        assert.match(element('discovery-error').innerText, /Choose 1, 2, 3, or 5/);
    }
    assert.equal(requests.length, 0);
});

const customService = {
    id: `custom_${'a'.repeat(32)}`, name: 'Bookkeeping', builtin: false,
    roles: ['Bookkeeper', 'Accounts payable clerk'], title_phrases: ['bookkeeper', 'accounts payable']
};

test('saved service is selected immediately and drives exact role budget, filters and labels', async () => {
    const { context, element, checked, requests } = setup('discovery');
    element('new-service-name').value = 'Bookkeeping';
    element('new-service-roles').value = 'Bookkeeper\nAccounts payable clerk';
    element('new-service-phrases').value = 'bookkeeper\naccounts payable';
    context.fetch = async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => url === '/api/hiring-services'
            ? { status: 'success', service: customService }
            : { status: 'success', run_id: 'custom-run' } };
    };
    await context.saveHiringService();
    assert.equal(requests[0].url, '/api/hiring-services');
    assert.equal(requests[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        name: 'Bookkeeping', roles: customService.roles, title_phrases: customService.title_phrases
    });
    assert.match(element('hiring-service-save-message').innerText, /Saved Bookkeeping/);
    assert.equal(element('new-service-name').value, '');
    assert.equal(element('btn-save-hiring-service').disabled, false);
    const label = element('custom-hiring-services').children[0];
    assert.equal(label.children[0].value, customService.id);
    assert.equal(label.children[0].checked, true);
    assert.equal(context.hiringServiceLabel(customService.id), 'Bookkeeping');
    context.setDiscoveryMode('jobs');
    assert.match(element('job-search-budget').innerText, /6 Google Jobs \+ 6 LinkedIn Jobs API requests; 12 total/);
    checked['job-service'] = [];
    element('job-pages-input').value = '3';
    context.updateHiringSearchBudget();
    assert.match(element('job-search-budget').innerText, /6 Google Jobs \+ 6 LinkedIn Jobs API requests; 12 total/);
    await context.submitActiveDiscovery();
    assert.deepEqual(JSON.parse(requests[1].options.body), {
        service_types: [customService.id], country: '', pages_per_role: 3
    });
});

test('service reload restores persisted definitions without duplicate controls or lost selections', async () => {
    const { context, element } = setup('discovery');
    const builtins = vm.runInContext('[...hiringServiceCatalog.values()]', context);
    context.fetch = async () => ({ ok: true, json: async () => ({
        status: 'success', services: [...builtins, customService]
    }) });
    await context.loadHiringServices();
    assert.equal(element('custom-hiring-services').children.length, 1);
    assert.equal(element('custom-hiring-services').children[0].children[0].checked, false);
    element('custom-hiring-services').children[0].children[0].checked = true;
    await context.loadHiringServices();
    assert.equal(element('custom-hiring-services').children.length, 1);
    assert.equal(element('custom-hiring-services').children[0].children[0].checked, true);
    assert.equal(element('hiring-services-error').classList.contains('hidden'), true);
    const fresh = setup('discovery');
    fresh.context.fetch = context.fetch;
    await fresh.context.loadHiringServices();
    assert.equal(fresh.context.hiringServiceLabel(customService.id), 'Bookkeeping');
});

test('service loading errors are explicit and malformed catalogs do not replace current definitions', async () => {
    const { context, element } = setup('jobs');
    for (const body of [
        { status: 'error', message: 'QA database unavailable' },
        { status: 'success', services: [null] },
        { status: 'success', services: [{ ...customService, roles: 'invalid' }] },
    ]) {
        context.fetch = async () => ({ ok: body.status === 'success', json: async () => body });
        await context.loadHiringServices();
        assert.equal(element('hiring-services-error').classList.contains('hidden'), false);
        assert.equal(element('btn-reload-hiring-services').classList.contains('hidden'), false);
        assert.match(element('hiring-services-error').innerText, /Reload the service list/);
        assert.equal(vm.runInContext('hiringServiceCatalog.size', context), 5);
    }
    context.fetch = async () => { throw new Error('Network unavailable'); };
    await context.loadHiringServices();
    assert.match(element('hiring-services-error').innerText, /Network unavailable/);
});

test('service save rejects invalid input and retains values on conflict or ambiguous network failure', async () => {
    const { context, element, requests } = setup('discovery');
    await context.saveHiringService();
    assert.equal(requests.length, 0);
    element('new-service-name').value = 'Bookkeeping';
    element('new-service-roles').value = 'Bookkeeper';
    element('new-service-phrases').value = 'bookkeeper';
    for (const fails of [false, true]) {
        context.fetch = async () => {
            if (fails) throw new Error('Network timeout');
            return { ok: false, json: async () => ({ status: 'error', message: 'Service already exists' }) };
        };
        await context.saveHiringService();
        assert.match(element('hiring-service-save-message').innerText, /Reload the service list before retrying/);
        assert.equal(element('new-service-name').value, 'Bookkeeping');
        assert.equal(element('btn-save-hiring-service').disabled, false);
        assert.equal(element('custom-hiring-services').children.length, 0);
    }
});

test('overlapping catalog requests are blocked and catalog refresh preserves a drafted email edit', async () => {
    const { context, element } = setup('jobs');
    const builtins = vm.runInContext('[...hiringServiceCatalog.values()]', context);
    let finish;
    let calls = 0;
    context.fetch = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
    const pending = context.loadHiringServices();
    await context.loadHiringServices();
    await context.saveHiringService();
    assert.equal(calls, 1);
    assert.match(element('hiring-service-save-message').innerText, /Wait for/);
    vm.runInContext(`
        activeQueueView = 'jobs';
        currentLeads = [{ id: 7, job_title: 'Bookkeeper', lead_type: '${customService.id}', email: 'old@example.test' }];
        selectedLeadId = 7;
    `, context);
    element('job-modal-email-input').value = 'unsaved@example.test';
    finish({ ok: true, json: async () => ({ status: 'success', services: [...builtins, customService] }) });
    await pending;
    assert.equal(element('job-modal-service').innerText, 'Bookkeeping');
    assert.equal(element('job-modal-email-input').value, 'unsaved@example.test');
});

test('unknown service definitions block estimates and paid discovery, names are rendered as text', async () => {
    const { context, element, checked, requests } = setup('discovery');
    checked['job-service'] = ['custom_unknown'];
    context.setDiscoveryMode('jobs');
    assert.match(element('job-search-budget').innerText, /definition is unavailable/);
    await context.submitActiveDiscovery();
    assert.equal(requests.length, 0);
    assert.match(element('discovery-error').innerText, /definition is unavailable/);
    checked['job-service'] = [];
    const hostile = { ...customService, name: '<img src=x onerror=alert(1)>' };
    context.fetch = async () => ({ ok: true, json: async () => ({ status: 'success', service: hostile }) });
    element('new-service-name').value = hostile.name;
    element('new-service-roles').value = 'Bookkeeper';
    element('new-service-phrases').value = 'bookkeeper';
    await context.saveHiringService();
    assert.match(element('custom-hiring-services').children[0].children[1].innerText, /<img src=x/);
    vm.runInContext(`
        activeQueueView = 'jobs';
        queueFilterOptions = { service: [{ value: '${hostile.id}', label: '${hostile.id}', count: 1 }] };
        renderQueueFilterOptions();
    `, context);
    assert.equal(element('filter-lead-type').children[1].innerText, `${hostile.name} (1)`);
    assert.equal(element('filter-lead-type').children[1].innerHTML, undefined);
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

function pollingSetup(run, page = 'prospects') {
    const fixture = setup(page);
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

test('hiring status polls its own endpoint and redirects to Job Leads only on success', async () => {
    const { context } = pollingSetup({ state: 'succeeded', total: 1, completed: 1, errors: [], logs: [] }, 'discovery');
    vm.runInContext("discoveryResultsView = 'jobs';", context);
    let requested;
    let destination;
    context.window.location.assign = url => { destination = url; };
    const original = context.fetch;
    context.fetch = async url => { requested = url; return original(url); };
    await context.pollLocalDiscoveryRun('qa-run');
    assert.equal(requested, '/api/discovery/job-runs/qa-run');
    assert.equal(destination, '/job-leads');
});

test('hiring failures and stop-waiting keep the user on the form with correct result guidance', async () => {
    const { context, element } = pollingSetup({
        state: 'failed', total: 2, completed: 2, errors: ['video_editing: quota exceeded'], logs: []
    });
    vm.runInContext("discoveryResultsView = 'jobs';", context);
    await context.pollLocalDiscoveryRun('qa-run');
    assert.match(element('discovery-error').innerText, /quota exceeded/);
    assert.equal(context.queueRefreshed, undefined);
    context.stopWaitingForLocalDiscovery();
    assert.match(element('discovery-error').innerText, /Job Leads/);
});

function sessionStorageStub() {
    const store = new Map();
    return {
        store,
        getItem: key => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: key => store.delete(key)
    };
}

test('a run that saved leads despite warnings is reported as success, not failure', async () => {
    const { context, element } = pollingSetup({
        state: 'succeeded', total: 2, completed: 2, saved: 12,
        errors: [], warnings: ['google_jobs / video_editing: Contact page timed out'], logs: []
    });
    context.sessionStorage = sessionStorageStub();
    await context.pollLocalDiscoveryRun('qa-run');
    // Leads were saved, so the results must still load.
    assert.equal(context.queueRefreshed, true);
    assert.equal(element('discovery-error').innerText, '', 'no failure message should be shown');
    const notice = element('queue-notice');
    assert.match(notice.innerText, /12 leads saved/);
    assert.match(notice.innerText, /1 step had problems/);
    assert.match(notice.innerText, /Contact page timed out/);
    assert.equal(notice.classList.contains('queue-notice-warn'), true);
});

test('a clean run reports the saved count without a warning style', async () => {
    const { context, element } = pollingSetup({
        state: 'succeeded', total: 1, completed: 1, saved: 1, errors: [], warnings: [], logs: []
    });
    context.sessionStorage = sessionStorageStub();
    await context.pollLocalDiscoveryRun('qa-run');
    const notice = element('queue-notice');
    assert.match(notice.innerText, /1 lead saved/);
    assert.equal(notice.classList.contains('queue-notice-warn'), false);
});

test('progress shows how many leads have been saved so far', async () => {
    const { context, element } = pollingSetup({
        state: 'running', total: 4, completed: 2, saved: 7, errors: [], warnings: [], logs: []
    });
    await context.pollLocalDiscoveryRun('qa-run');
    assert.match(element('loading-overlay-text').innerText, /2 of 4 .* finished\. 7 leads saved so far\./);
    assert.equal(element('discovery-progress-scans').innerText, '2 / 4');
    assert.equal(element('discovery-progress-saved').innerText, 7);
    assert.equal(element('discovery-progress-bar').max, 4);
    assert.equal(element('discovery-progress-bar').value, 2);
    assert.equal(element('discovery-progress-bar').attributes['aria-valuetext'], '2 of 4 searches completed');
    assert.equal(element('discovery-progress-metrics').classList.contains('hidden'), false);
});

test('progress resets between operations and does not invent saved counts or percentages', () => {
    const { context, element } = setup();
    context.updateDiscoveryProgress({ total: 4, completed: 3, saved: 9 });
    context.updateDiscoveryProgress();
    assert.equal(element('discovery-progress-metrics').classList.contains('hidden'), true);
    assert.equal(element('discovery-progress-bar').value, '');
    assert.equal(element('discovery-progress-bar').attributes['aria-valuetext'], undefined);
    context.updateDiscoveryProgress({ total: 0, completed: 0 });
    assert.equal(element('discovery-progress-scans').innerText, '0 / 0');
    assert.equal(element('discovery-progress-saved').innerText, '\u2014');
    assert.equal(element('discovery-progress-bar').value, '');
});

test('premium activity feed keeps provider content as text', () => {
    const { context, element } = setup();
    context.appendTerminalLine(element('terminal-stream-log'), '<img src=x onerror=alert(1)>');
    const line = element('terminal-stream-log').children[0];
    assert.equal(line.className, 'discovery-log-line');
    assert.equal(line.innerText, '<img src=x onerror=alert(1)>');
    assert.equal(line.innerHTML, undefined);
    assert.match(html, /aria-labelledby="discovery-progress-title"/);
    assert.match(html, /id="loading-overlay-text" role="status" aria-live="polite"/);
});

test('a status payload without the new fields still completes safely', async () => {
    const { context, element } = pollingSetup({
        state: 'succeeded', total: 1, completed: 1, errors: [], logs: []
    });
    context.sessionStorage = sessionStorageStub();
    await context.pollLocalDiscoveryRun('qa-run');
    assert.equal(context.queueRefreshed, true);
    assert.doesNotMatch(element('loading-overlay-text').innerText, /saved so far/);
});

test('the discovery summary survives the redirect to the results page', async () => {
    const { context } = pollingSetup({
        state: 'succeeded', total: 1, completed: 1, saved: 5, errors: [], warnings: [], logs: []
    }, 'discovery');
    const storage = sessionStorageStub();
    context.sessionStorage = storage;
    vm.runInContext("window = { location: { assign(target) { globalThis.destination = target; } } };", context);
    await context.pollLocalDiscoveryRun('qa-run');
    assert.equal(context.destination, '/prospects');
    // The summary is handed over rather than flashed and lost.
    assert.match(storage.getItem('asuratech-discovery-outcome'), /"saved":5/);
});

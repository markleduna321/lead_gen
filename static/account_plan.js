(() => {
    let account;
    let quoting = false;
    let summaryRequest;
    const status = document.getElementById('account-plan-status');
    const creditCard = document.getElementById('sidebar-credits');
    const formatCredits = value => new Intl.NumberFormat(undefined, {maximumFractionDigits: 2}).format(Number(value));
    function renderCredits() {
        if (!creditCard) return;
        const usage = account.usage;
        const percent = Number(usage.percent);
        document.getElementById('credit-plan').textContent = account.plan.name;
        document.getElementById('credit-balance').textContent = formatCredits(account.balance);
        document.getElementById('credit-usage-values').textContent =
            `${formatCredits(usage.used)} / ${formatCredits(usage.total)} used`;
        document.getElementById('credit-usage-percent').textContent = `${percent.toFixed(1)}%`;
        const meter = document.getElementById('credit-usage-meter');
        meter.value = percent;
        meter.setAttribute('aria-valuetext', `${percent.toFixed(1)}% used this cycle, including reserved credits`);
        document.getElementById('credit-ring-value').setAttribute('stroke-dasharray',
            `${account.plan.active ? 100 - percent : 0} 100`);
        const date = usage.next_date ? new Date(usage.next_date).toLocaleDateString(undefined,
            {month: 'short', day: 'numeric', year: 'numeric'}) : '';
        document.getElementById('credit-renewal').textContent = !account.plan.active
            ? 'Activate a plan to start searching'
            : `${usage.next_event === 'refresh' ? 'Credits refresh' : 'Plan expires'} ${date}`;
        document.getElementById('credit-live-status').textContent = account.plan.active
            ? 'Live shared wallet · includes reservations' : 'No active subscription';
        const link = document.getElementById('credit-plan-link');
        link.textContent = !account.plan.active ? 'Choose a plan'
            : account.plan.id === 'enterprise' ? 'View plan details'
            : account.owner ? 'Upgrade plan' : 'Compare plans';
        link.href = account.owner ? '/billing' : '/plans';
        creditCard.dataset.state = account.plan.active && percent >= 80 ? 'low' : 'ready';
        creditCard.setAttribute('aria-busy', 'false');
        creditCard.title = `${formatCredits(account.balance)} search credits available; ${percent.toFixed(1)}% used this cycle`;
    }
    function creditError(error) {
        if (!creditCard) return;
        creditCard.dataset.state = 'error';
        creditCard.setAttribute('aria-busy', 'false');
        document.getElementById('credit-live-status').textContent = `Wallet unavailable: ${error.message}`;
        creditCard.title = 'Wallet unavailable. Displayed values may be out of date.';
    }
    document.querySelectorAll('input[name="scan-source"], #custom-service-editor input, '
        + '#custom-service-editor textarea, #custom-service-editor button, #btn-discovery-submit')
        .forEach(input => { input.disabled = true; });
    const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
    const upgrade = (container, text) => {
        const link = document.createElement('a');
        link.href = '/plans';
        link.textContent = text;
        link.className = 'settings-hint';
        container.appendChild(link);
    };
    window.applyAccountCapabilities = () => {
        if (!account) return;
        const hiringSelected = document.getElementById('discovery-tab-jobs')?.getAttribute('aria-selected') === 'true';
        const submit = document.getElementById('btn-discovery-submit');
        if (submit) submit.disabled = !account.plan.active || (hiringSelected && !account.plan.hiring_pages);
        document.querySelectorAll('#custom-hiring-services input').forEach(input => {
            input.disabled = !account.plan.custom_services;
            if (input.disabled) input.checked = false;
        });
        document.querySelectorAll('#custom-service-editor input, #custom-service-editor textarea, '
            + '#custom-service-editor button').forEach(input => {
            input.disabled = !account.plan.custom_services;
        });
    };
    async function fetchAccountSummary() {
        const response = await fetch('/api/account-plan', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
        const data = await response.json();
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Plan could not be loaded.');
        account = data.account;
        status.textContent = `${account.plan.name} · ${account.balance} shared search credits`;
        status.setAttribute('role', 'status');
        renderCredits();
        const navigation = document.getElementById('nav-user-management');
        navigation.hidden = !account.can_manage_team;
        if (document.body.dataset.page === 'user-management') {
            navigation.setAttribute('aria-current', 'page');
            navigation.classList.add('is-active');
        }
        const billingNavigation = document.getElementById('nav-billing');
        if (billingNavigation) billingNavigation.hidden = !account.owner;
    }
    function updateAccountSummary() {
        if (!summaryRequest) summaryRequest = fetchAccountSummary().finally(() => { summaryRequest = null; });
        return summaryRequest;
    }
    const loaded = (async () => {
        try {
            await updateAccountSummary();
            document.querySelectorAll('input[name="scan-source"]').forEach(input => {
                const allowed = account.plan.sources.includes(input.value);
                input.disabled = !allowed;
                if (!allowed) {
                    input.checked = false;
                    upgrade(input.parentElement, input.value === 'yelp'
                        ? ' Upgrade to Solo to unlock' : ' Upgrade to Small Team to unlock');
                }
            });
            const jobs = document.getElementById('discovery-panel-jobs');
            if (jobs && !account.plan.hiring_pages) {
                jobs.querySelectorAll('input, select, textarea, button').forEach(input => { input.disabled = true; });
                upgrade(jobs, 'Upgrade to Small Team to unlock Hiring Companies discovery');
            }
            const custom = document.getElementById('custom-service-editor');
            if (custom && !account.plan.custom_services) {
                custom.querySelectorAll('input, textarea, button').forEach(input => { input.disabled = true; });
                upgrade(custom, 'Upgrade to Enterprise to add custom hiring services');
            }
            const pages = document.getElementById('job-pages-input');
            if (pages) Array.from(pages.options).forEach(option => {
                option.disabled = Number(option.value) > account.plan.hiring_pages;
            });
            const info = document.getElementById('plan-discovery-policy');
            if (info) info.textContent = `Local discovery: 10 credits per industry; maximum ${account.plan.industries} industries. `
                + `Hiring: 15 credits per role-page. Completed zero-new-lead submissions receive a 70% credit refund. `
                + 'All members share the owner balance and daily cap. Leads remain private.';
            window.applyAccountCapabilities();
        } catch (error) {
            status.textContent = `Plan unavailable: ${error.message}`;
            status.setAttribute('role', 'alert');
            creditError(error);
            const submit = document.getElementById('btn-discovery-submit');
            if (submit) submit.disabled = true;
        }
    })();

    window.refreshAccountPlan = async () => {
        await loaded;
        try {
            await updateAccountSummary();
            window.applyAccountCapabilities();
        } catch (error) {
            status.textContent = `Balance refresh failed: ${error.message}`;
            status.setAttribute('role', 'alert');
            creditError(error);
        }
    };
    setInterval(() => {
        if (!document.hidden) window.refreshAccountPlan();
    }, 30000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) window.refreshAccountPlan();
    });
    window.addEventListener('focus', () => {
        if (!document.hidden) window.refreshAccountPlan();
    });

    window.preparePaidSearch = async (endpoint, payload) => {
        if (quoting) return false;
        quoting = true;
        try {
            await loaded;
            if (!account) throw new Error('Your plan could not be loaded. Reload before searching.');
            const mode = endpoint.endsWith('discover-jobs') ? 'hiring' : 'local';
            const response = await fetch('/api/discovery/quote', {
                method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()},
                body: JSON.stringify({mode, search: payload})
            });
            const data = await response.json();
            if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Search quote failed.');
            const quote = data.quote;
            if (!quote.sufficient) throw new Error(`This search costs ${quote.charge} credits; the shared balance is ${quote.balance}.`);
            if (!confirm(`This search reserves ${quote.charge} shared search credits.\n`
                + `Balance: ${quote.balance} credits.\n${quote.policy}\n`
                + `Zero-new-lead refund: ${quote.zero_result_refund} credits.\nContinue?`)) return false;
            const fingerprint = JSON.stringify([endpoint, payload]);
            const pending = JSON.parse(sessionStorage.getItem('paid-discovery-request') || 'null');
            const key = pending && pending.fingerprint === fingerprint ? pending.key : crypto.randomUUID();
            sessionStorage.setItem('paid-discovery-request', JSON.stringify({key, fingerprint}));
            window.paidDiscoveryHeaders = {'Idempotency-Key': key, 'X-CSRF-Token': csrf()};
            return true;
        } catch (error) {
            if (typeof showDiscoveryError === 'function') showDiscoveryError(error.message);
            else status.textContent = error.message;
            return false;
        } finally {
            quoting = false;
        }
    };
    window.completePaidSearchRequest = () => {
        sessionStorage.removeItem('paid-discovery-request');
    };
})();

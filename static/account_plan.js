(() => {
    let account;
    let quoting = false;
    let summaryRequest;
    let confirmationControls = [];
    let resolveSearchConfirmation;
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
        link.href = '/billing';
        link.textContent = text;
        link.className = 'settings-hint';
        container.appendChild(link);
    };
    function updateIndustryLimit() {
        const inputs = Array.from(document.querySelectorAll('input[name="biz-type"]'));
        const limit = account.plan.industries;
        let selected = inputs.filter(input => input.checked);
        if (selected.length > limit) {
            selected.slice(limit).forEach(input => { input.checked = false; });
            selected = inputs.filter(input => input.checked);
        }
        inputs.forEach(input => {
            input.disabled = !account.plan.active || (selected.length >= limit && !input.checked);
        });
        const counter = document.getElementById('industry-limit-status');
        if (counter) counter.textContent = `${selected.length} of ${limit} ${limit === 1 ? 'industry' : 'industries'} selected`;
    }
    window.updateDiscoverySearchEstimate = () => {
        const estimate = document.getElementById('discovery-search-estimate');
        if (!estimate || !account) return;
        const jobsSelected = document.getElementById('discovery-tab-jobs')?.getAttribute('aria-selected') === 'true';
        if (jobsSelected) {
            if (typeof window.updateHiringSearchBudget === 'function') window.updateHiringSearchBudget();
            return;
        }
        const limit = account.plan.industries;
        const price = Number(account.plan.local_price);
        const selected = document.querySelectorAll('input[name="biz-type"]:checked').length;
        if (!account.plan.active) {
            estimate.textContent = 'Activate a plan to start searching.';
        } else if (!selected) {
            estimate.textContent = `Live estimate: ${formatCredits(price)} search credits per industry. Choose up to ${limit} ${limit === 1 ? 'industry' : 'industries'}.`;
        } else {
            estimate.textContent = `Local search estimate: ${formatCredits(selected * price)} search credits for ${selected} ${selected === 1 ? 'industry' : 'industries'} (${formatCredits(price)} each; plan limit ${limit}).`;
        }
    };
    window.applyAccountCapabilities = () => {
        if (!account) return;
        const hiringSelected = document.getElementById('discovery-tab-jobs')?.getAttribute('aria-selected') === 'true';
        const submit = document.getElementById('btn-discovery-submit');
        if (submit) submit.disabled = !account.plan.active || (hiringSelected && !account.plan.hiring_pages);
        updateIndustryLimit();
        document.querySelectorAll('#custom-hiring-services input').forEach(input => {
            input.disabled = !account.plan.custom_services;
            if (input.disabled) input.checked = false;
        });
        document.querySelectorAll('#custom-service-editor input, #custom-service-editor textarea, '
            + '#custom-service-editor button').forEach(input => {
            input.disabled = !account.plan.custom_services;
        });
        const pages = document.getElementById('job-pages-input');
        if (pages) {
            Array.from(pages.options).forEach(option => {
                option.disabled = Number(option.value) > account.plan.hiring_pages;
            });
            const available = Array.from(pages.options).filter(option => !option.disabled);
            if (account.plan.active && available.length && Number(pages.value) > account.plan.hiring_pages) {
                pages.value = available[available.length - 1].value;
            }
        }
        const pageLimit = document.getElementById('job-pages-limit-status');
        if (pageLimit) pageLimit.textContent = account.plan.hiring_pages
            ? `Your plan supports up to ${account.plan.hiring_pages} pages per role.`
            : 'Hiring discovery requires a Small Team or Enterprise plan.';
        window.updateDiscoverySearchEstimate();
        if (resolveSearchConfirmation) lockSearchControls();
    };
    document.addEventListener('change', event => {
        const target = event.target;
        if (target?.name === 'biz-type') window.applyAccountCapabilities();
        else if (target?.name === 'job-service' || target?.id === 'job-pages-input') {
            window.updateDiscoverySearchEstimate();
        }
    });
    async function fetchAccountSummary() {
        const response = await fetch('/api/account-plan', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
        const data = await response.json();
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Plan could not be loaded.');
        account = data.account;
        window.discoverySearchPrices = {
            local: Number(account.plan.local_price),
            hiring: Number(account.plan.hiring_price)
        };
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
            const info = document.getElementById('plan-discovery-policy');
            const industryWord = account.plan.industries === 1 ? 'industry' : 'industries';
            if (info) info.textContent = `Local discovery: ${formatCredits(account.plan.local_price)} credits per industry; maximum ${account.plan.industries} ${industryWord}. `
                + `Hiring: ${formatCredits(account.plan.hiring_price)} credits per role-page. Completed zero-new-lead submissions receive a 70% credit refund. `
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
            const confirmation = document.getElementById('search-cost-confirmation');
            const summary = document.getElementById('search-cost-summary');
            if (!confirmation || !summary) throw new Error('Search confirmation is unavailable. Reload before trying again.');
            summary.textContent = `This search will reserve ${quote.charge} search credits from your shared balance of ${quote.balance}. `
                + `${quote.policy} Zero-new-lead refund: ${quote.zero_result_refund} credits.`;
            confirmation.classList.remove('hidden');
            lockSearchControls();
            const accepted = await new Promise(resolve => {
                resolveSearchConfirmation = resolve;
            });
            if (!accepted) return false;
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
    function settleSearchConfirmation(accepted) {
        if (!resolveSearchConfirmation) return;
        const confirmation = document.getElementById('search-cost-confirmation');
        confirmation.classList.add('hidden');
        confirmationControls.forEach(([control, wasDisabled]) => { control.disabled = wasDisabled; });
        confirmationControls = [];
        const resolve = resolveSearchConfirmation;
        resolveSearchConfirmation = null;
        resolve(accepted);
    }
    function lockSearchControls() {
        if (!confirmationControls.length) {
            confirmationControls = Array.from(document.querySelectorAll(
                '#discovery-tab-local, #discovery-tab-jobs, #discovery-panel-local input, '
                + '#discovery-panel-local select, #discovery-panel-local textarea, #discovery-panel-local button, '
                + '#discovery-panel-jobs input, #discovery-panel-jobs select, #discovery-panel-jobs textarea, '
                + '#discovery-panel-jobs button, #btn-discovery-submit'
            )).map(control => [control, control.disabled]);
        }
        confirmationControls.forEach(([control]) => { control.disabled = true; });
    }
    window.cancelPaidSearchConfirmation = () => settleSearchConfirmation(false);
    document.getElementById('search-cost-continue')?.addEventListener('click', () => settleSearchConfirmation(true));
    document.getElementById('search-cost-cancel')?.addEventListener('click', () => settleSearchConfirmation(false));
})();

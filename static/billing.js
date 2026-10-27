(() => {
    let state = JSON.parse(document.getElementById('billing-data').textContent);
    const prices = JSON.parse(document.getElementById('billing-prices').textContent);
    const names = {solo: 'Solo', small_team: 'Small Team', enterprise: 'Enterprise'};
    const message = document.getElementById('billing-message');
    let busy = false;
    const money = amount => new Intl.NumberFormat(undefined, {style: 'currency', currency: 'PHP',
        maximumFractionDigits: 0}).format(amount / 100);
    const open = order => ['creating', 'pending', 'unknown', 'review'].includes(order.status);
    const notice = (text, error = false) => {
        message.textContent = text;
        message.className = error ? 'queue-notice queue-notice-warn' : 'queue-notice';
        message.hidden = false;
    };
    const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
    const safeCheckout = value => {
        try {
            const url = new URL(value);
            return url.protocol === 'https:' && url.host === 'checkout.paymongo.com'
                && !url.username && !url.password;
        } catch { return false; }
    };
    async function api(url, method = 'GET', body, key) {
        const response = await fetch(url, {
            method, cache: 'no-store', signal: AbortSignal.timeout(35000),
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf(),
                ...(key ? {'Idempotency-Key': key} : {})},
            body: body === undefined ? undefined : JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Billing request failed.');
        return data;
    }
    function render() {
        const account = state.account;
        const blocked = state.orders.some(open);
        document.getElementById('billing-trial-offer').hidden = !state.can_start_trial;
        document.getElementById('billing-start-trial').disabled = busy || blocked || !state.can_start_trial;
        const months = Number(document.getElementById('billing-months').value);
        document.getElementById('billing-current-plan').textContent = account.plan.name;
        document.getElementById('billing-current-balance').textContent = `${Number(account.balance).toLocaleString()} shared search credits available`;
        document.getElementById('billing-current-status').textContent = account.plan.active ? 'Active' : 'Inactive';
        const date = account.ends_at ? new Date(account.ends_at).toLocaleDateString() : '';
        document.getElementById('billing-expiry').textContent = date ? `Plan expiry: ${date}` : 'No paid billing period yet';
        document.querySelectorAll('[data-price-plan]').forEach(node => {
            node.textContent = `${money(prices[node.dataset.pricePlan][months])} / ${months === 12 ? 'year' : 'month'}`;
        });
        document.querySelectorAll('[data-price-note]').forEach(node => {
            node.textContent = months === 12 ? 'One payment for 12 months. Credits granted monthly.'
                : 'One-time monthly payment';
        });
        document.querySelectorAll('[data-buy-plan]').forEach(button => {
            const current = account.plan.active && account.plan.id !== 'trial';
            button.disabled = busy || !state.ready || blocked || current;
            button.textContent = current && account.plan.id === button.dataset.buyPlan ? 'Current plan'
                : current ? 'Contact support to change' : `Choose ${names[button.dataset.buyPlan]}`;
        });
        const pack = prices[account.plan.id];
        const eligible = account.plan.active && !!pack;
        const topup = document.getElementById('billing-buy-topup');
        topup.disabled = busy || !state.ready || blocked || !eligible;
        topup.textContent = eligible ? `Buy ${pack.pack.toLocaleString()} credits · ${money(pack.topup)}` : 'Active paid plan required';
        document.getElementById('billing-topup-description').textContent = eligible
            ? `${names[account.plan.id]} pack: ${pack.pack.toLocaleString()} shared search credits for ${money(pack.topup)}.`
            : 'Top-ups become available once your paid plan is active.';
        document.getElementById('billing-months').disabled = busy;
        document.getElementById('billing-refresh').disabled = busy;
        const rows = document.getElementById('billing-orders');
        rows.replaceChildren();
        document.getElementById('billing-empty').hidden = state.orders.length > 0;
        state.orders.forEach(order => {
            const row = document.createElement('tr');
            const description = order.kind === 'topup' ? `${names[order.plan_id]} credit pack`
                : `${names[order.plan_id]} · ${order.months === 12 ? 'Annual' : 'Monthly'}`;
            [description, new Date(order.created_at).toLocaleDateString(), money(order.amount),
                {paid: 'Paid · activated', review: 'Paid · review required', creating: 'Creating checkout',
                    pending: 'Awaiting payment', unknown: 'Needs reconciliation', expired: 'Cancelled / expired',
                    failed: 'Failed'}[order.status] || order.status].forEach((value, index) => {
                const cell = document.createElement('td');
                cell.textContent = value;
                if (index === 0) {
                    const reference = document.createElement('small');
                    reference.textContent = `${order.livemode ? '' : 'TEST · '}${order.id}`;
                    cell.appendChild(reference);
                    if (order.message) {
                        const detail = document.createElement('small');
                        detail.textContent = order.message;
                        cell.appendChild(detail);
                    }
                }
                row.appendChild(cell);
            });
            const actions = document.createElement('td');
            if (order.status === 'pending') {
                if (safeCheckout(order.checkout_url)) {
                    const resume = document.createElement('a');
                    resume.href = order.checkout_url;
                    resume.className = 'secondary-button';
                    resume.textContent = 'Continue payment';
                    actions.appendChild(resume);
                }
                [['Recheck', false], ['Cancel', true]].forEach(([label, cancel]) => {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = 'secondary-button';
                    button.textContent = label;
                    button.disabled = busy;
                    button.addEventListener('click', () => action(async () => {
                        if (cancel && !confirm('Expire this checkout? If payment is processing, PayMongo may refuse cancellation.')) return;
                        const result = await api(`/api/billing/orders/${encodeURIComponent(order.id)}/${cancel ? 'cancel' : 'recheck'}`, 'POST');
                        reportOrder(result.order);
                        await refresh();
                    }));
                    actions.appendChild(button);
                });
            }
            row.appendChild(actions);
            rows.appendChild(row);
        });
    }
    function reportOrder(order) {
        if (order.status === 'paid') {
            sessionStorage.removeItem('billing-purchase');
            notice('Payment confirmed. Your plan or shared credit pack is now active.');
        } else if (order.status === 'expired' || order.status === 'failed') {
            sessionStorage.removeItem('billing-purchase');
            notice('Checkout expired or failed. No access was granted from this order.');
        }
        else if (order.status === 'review') notice(order.message, true);
        else notice('Payment is not confirmed yet. Do not pay again if you already completed the transfer; recheck shortly.');
    }
    async function refresh() {
        state = (await api('/api/billing')).billing;
        render();
        await window.refreshAccountPlan?.();
    }
    async function action(work, payment = true) {
        if (busy) return;
        busy = true;
        render();
        try { await work(); }
        catch (error) {
            notice(payment ? `${error.message} Check purchase history before retrying or making another payment.`
                : `${error.message} Refresh Billing to check your trial status before retrying.`, true);
            try { await refresh(); }
            catch (refreshError) {
                notice(payment
                    ? `${error.message} History refresh also failed: ${refreshError.message}. Do not pay again until the order is checked.`
                    : `${error.message} Trial status refresh also failed: ${refreshError.message}. Refresh Billing before retrying.`, true);
            }
        } finally { busy = false; render(); }
    }
    async function buy(kind, plan_id) {
        const months = kind === 'topup' ? 1 : Number(document.getElementById('billing-months').value);
        const amount = prices[plan_id][kind === 'topup' ? 'topup' : months];
        if (!confirm(`Pay ${money(amount)} using QR Ph via PayMongo?\nThis is a one-time payment with no automatic renewal.`
            + (kind === 'plan' && state.account.plan.id === 'trial'
                ? '\nYour paid plan starts immediately on confirmation and replaces unused trial credits.' : ''))) return;
        const payload = {kind, plan_id, months};
        const fingerprint = JSON.stringify(payload);
        const previous = JSON.parse(sessionStorage.getItem('billing-purchase') || 'null');
        const key = previous?.fingerprint === fingerprint ? previous.key : crypto.randomUUID();
        sessionStorage.setItem('billing-purchase', JSON.stringify({fingerprint, key}));
        const {order} = await api('/api/billing/checkout', 'POST', payload, key);
        if (order.status === 'pending' && safeCheckout(order.checkout_url)) {
            window.location.assign(order.checkout_url);
        } else {
            reportOrder(order);
            await refresh();
        }
    }
    document.getElementById('billing-months').addEventListener('change', render);
    document.querySelectorAll('[data-buy-plan]').forEach(button => {
        button.addEventListener('click', () => action(() => buy('plan', button.dataset.buyPlan)));
    });
    document.getElementById('billing-buy-topup').addEventListener('click',
        () => action(() => buy('topup', state.account.plan.id)));
    document.getElementById('billing-refresh').addEventListener('click', () => action(refresh));
    document.getElementById('billing-start-trial').addEventListener('click', () => action(async () => {
        await api('/api/billing/trial', 'POST');
        notice('Your 7-day free trial is active with 30 search credits. Start with Google Maps discovery.');
        await refresh();
    }, false));
    render();
    const query = new URLSearchParams(window.location.search);
    const returnOrder = state.orders.find(order => order.id === query.get('order'));
    if (returnOrder && query.get('checkout')) {
        notice(query.get('checkout') === 'cancel'
            ? 'You returned without confirmation. Your checkout remains payable until it is cancelled below.'
            : 'Checking your payment with PayMongo. Returning here alone does not activate a plan.');
        action(async () => {
            if (returnOrder.status === 'pending') {
                const {order} = await api(`/api/billing/orders/${encodeURIComponent(returnOrder.id)}/recheck`, 'POST');
                reportOrder(order);
            } else reportOrder(returnOrder);
            await refresh();
        });
    }
})();

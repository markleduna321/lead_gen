(() => {
    const form = document.querySelector('.auth-form');
    const password = document.getElementById('password');
    const meter = document.getElementById('password-strength-meter');
    const strength = document.getElementById('password-strength');
    if (!form) return;

    const commonPasswords = new Set([
        'password', 'password1', 'password123', 'passw0rd', 'p@ssw0rd',
        '1234567890', '123456789', 'qwerty123', 'qwerty12345', 'letmein123',
        'welcome123', 'admin12345', 'iloveyou123', 'changeme123', 'monkey12345',
        'abc1234567', 'football123', 'dragon12345', 'sunshine123', 'trustno1'
    ]);

    function passwordStrength(value) {
        if (value.length < 10) return {score: 0, label: 'Too short'};
        const normalized = value.toLowerCase().trim();
        if (commonPasswords.has(normalized)
                || commonPasswords.has(normalized.replace(/[^a-z0-9]/g, ''))
                || /^(.)\1{9,}$/i.test(value)) {
            return {score: 1, label: 'Weak'};
        }
        const classes = [
            /[a-z]/.test(value), /[A-Z]/.test(value), /\d/.test(value), /[^A-Za-z0-9]/.test(value)
        ].filter(Boolean).length;
        const passphrase = value.length >= 16 && (value.toLowerCase().match(/[a-z]{3,}/g) || []).length >= 3;
        if (classes < 3 && !passphrase) return {score: 1, label: 'Weak'};
        if (classes === 4 && value.length >= 14) return {score: 4, label: 'Strong'};
        if (passphrase || (classes >= 3 && value.length >= 14)) return {score: 3, label: 'Good'};
        return {score: 2, label: 'Fair'};
    }

    function updatePasswordStrength() {
        if (!meter || !strength || !password) return;
        const result = passwordStrength(password.value);
        meter.value = result.score;
        meter.dataset.strength = result.label.toLowerCase().replace(/\s+/g, '-');
        strength.textContent = password.value
            ? `Password strength: ${result.label}`
            : 'Password strength: not checked';
    }

    form.addEventListener('input', event => {
        const field = event.target;
        if (field !== password && field.name !== 'confirm_password'
                && field.name !== 'email' && field.name !== 'display_name') return;
        const describedBy = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
        describedBy.filter(id => id.endsWith('-error')).forEach(id => {
            document.getElementById(id)?.remove();
        });
        if (field.hasAttribute('aria-invalid')) {
            field.removeAttribute('aria-invalid');
            field.removeAttribute('aria-describedby');
            if (field === password) {
                field.setAttribute('aria-describedby', 'password-hint password-strength');
            }
        }
        if (field === password) updatePasswordStrength();
    });

    updatePasswordStrength();
})();

const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');

function setThemePreference(preference, persist = true) {
    const validPreference = ['system', 'light', 'dark'].includes(preference) ? preference : 'system';
    document.documentElement.dataset.themePreference = validPreference;
    document.documentElement.dataset.theme = validPreference === 'system'
        ? (themeMedia.matches ? 'dark' : 'light') : validPreference;
    if (persist) {
        try {
            localStorage.setItem('asuratech-theme', validPreference);
        } catch (error) {
            console.warn('Theme preference could not be saved.', error);
        }
    }
    const selector = document.getElementById('theme-preference');
    if (selector) selector.value = validPreference;
}

let savedTheme = 'system';
try {
    savedTheme = localStorage.getItem('asuratech-theme') || 'system';
} catch (error) {
    console.warn('Theme preference could not be loaded.', error);
}
setThemePreference(savedTheme, false);
themeMedia.addEventListener('change', () => {
    if (document.documentElement.dataset.themePreference === 'system') setThemePreference('system', false);
});

function toggleSidebar(show) {
    const open = show === undefined ? !document.body.classList.contains('nav-open') : show;
    document.body.classList.toggle('nav-open', open);
    document.getElementById('menu-toggle').setAttribute('aria-expanded', String(open));
    document.getElementById('sidebar-backdrop').hidden = !open;
}

document.addEventListener('DOMContentLoaded', () => {
    setThemePreference(document.documentElement.dataset.themePreference, false);
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
        toggleSidebar(false);
        if (typeof toggleDiscoveryModal === 'function' && document.body.dataset.page !== 'discovery') {
            toggleDiscoveryModal(false);
        }
    }
});

// An expired session returns 401 JSON from every /api route. Without this the
// dashboard's fetch().json() calls fail with an opaque parse error instead of
// sending the operator back to sign in.
(function redirectOnExpiredSession() {
    if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
    const nativeFetch = window.fetch.bind(window);
    let redirecting = false;
    window.fetch = async function (...args) {
        const response = await nativeFetch(...args);
        if (response.status !== 401 || redirecting) return response;
        if (document.body && document.body.dataset.page === 'auth') return response;
        let expired = false;
        try {
            expired = (await response.clone().json()).code === 'auth_required';
        } catch (error) {
            expired = false; // Not our auth payload; leave it to the caller.
        }
        if (expired) {
            redirecting = true;
            const back = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.assign('/login?next=' + back);
        }
        return response;
    };
})();

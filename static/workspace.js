const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
const modalFocusableSelector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
let activeAccessibleModal = null;

function getModalElement(modal) {
    const element = typeof modal === 'string' ? document.getElementById(modal) : modal;
    if (!element) throw new Error('Accessible modal element was not found.');
    return element;
}

function getModalFocusableElements(modal) {
    return [...modal.querySelectorAll(modalFocusableSelector)].filter(element =>
        !element.disabled && !element.closest('[hidden], .hidden, [inert], [aria-hidden="true"]')
    );
}

function openAccessibleModal(modal, { onEscape, closeOnEscape = true } = {}) {
    const element = getModalElement(modal);
    if (activeAccessibleModal?.element === element) {
        activeAccessibleModal.onEscape = onEscape;
        activeAccessibleModal.closeOnEscape = closeOnEscape;
        return;
    }
    const previousFocus = activeAccessibleModal
        ? activeAccessibleModal.previousFocus : document.activeElement;
    if (activeAccessibleModal) closeAccessibleModal(activeAccessibleModal.element, { restoreFocus: false });

    activeAccessibleModal = {
        element,
        previousFocus,
        onEscape,
        closeOnEscape
    };
    element.classList.remove('hidden');
    element.setAttribute('role', 'dialog');
    element.setAttribute('aria-modal', 'true');
    const focusable = getModalFocusableElements(element);
    if (focusable.length) focusable[0].focus();
    else {
        element.setAttribute('tabindex', '-1');
        element.focus();
    }
}

function closeAccessibleModal(modal, { restoreFocus = true } = {}) {
    const element = getModalElement(modal);
    element.classList.add('hidden');
    if (activeAccessibleModal?.element !== element) return;

    const { previousFocus } = activeAccessibleModal;
    activeAccessibleModal = null;
    if (restoreFocus && previousFocus?.isConnected && typeof previousFocus.focus === 'function') {
        previousFocus.focus();
    }
}

function handleAccessibleModalKeydown(event) {
    const modalState = activeAccessibleModal;
    if (!modalState) return false;
    const { element, onEscape, closeOnEscape } = modalState;

    if (event.key === 'Escape') {
        event.preventDefault();
        if (closeOnEscape) {
            if (typeof onEscape === 'function') onEscape();
            else closeAccessibleModal(element);
        }
        return true;
    }
    if (event.key !== 'Tab') return false;

    const focusable = getModalFocusableElements(element);
    if (!focusable.length) {
        event.preventDefault();
        element.focus();
        return true;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!element.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
    return true;
}

function setThemePreference(preference, persist = true) {
    const validPreference = ['system', 'light', 'dark'].includes(preference) ? preference : 'system';
    document.documentElement.dataset.themePreference = validPreference;
    const theme = validPreference === 'system'
        ? (themeMedia.matches ? 'dark' : 'light') : validPreference;
    document.documentElement.dataset.theme = theme;
    const themeColor = document.getElementById('theme-color-meta');
    if (themeColor) themeColor.content = theme === 'dark' ? '#101619' : '#f5f7f9';
    if (persist) {
        try {
            localStorage.setItem('asuratech-theme', validPreference);
        } catch (error) {
            console.warn('Theme preference could not be saved.', error);
        }
    }
    const selector = document.getElementById('theme-preference');
    if (selector) selector.value = validPreference;
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
        const label = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
        toggle.setAttribute('aria-label', label);
        toggle.setAttribute('title', label);
    }
}

function toggleThemePreference() {
    setThemePreference(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
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

// Sidebar collapse state persistence (desktop rail mode)
(function initSidebarPreference() {
    try {
        if (localStorage.getItem('asuratech-sidebar-collapsed') === 'true') {
            document.documentElement.dataset.sidebar = 'collapsed';
        }
    } catch (error) {
        console.warn('Sidebar preference could not be loaded.', error);
    }
})();

function toggleSidebarCollapsed(force) {
    const isCurrentlyCollapsed = document.documentElement.dataset.sidebar === 'collapsed';
    const shouldCollapse = force !== undefined ? force : !isCurrentlyCollapsed;

    if (shouldCollapse) {
        document.documentElement.dataset.sidebar = 'collapsed';
    } else {
        delete document.documentElement.dataset.sidebar;
    }

    const collapseBtn = document.getElementById('sidebar-collapse');
    if (collapseBtn) {
        collapseBtn.setAttribute('aria-expanded', String(!shouldCollapse));
        const label = shouldCollapse ? 'Expand sidebar' : 'Collapse sidebar';
        collapseBtn.setAttribute('aria-label', label);
        collapseBtn.setAttribute('title', label);
    }

    try {
        localStorage.setItem('asuratech-sidebar-collapsed', shouldCollapse ? 'true' : 'false');
    } catch (error) {
        console.warn('Sidebar preference could not be saved.', error);
    }
}

function onSidebarBrandClick(event) {
    if (document.documentElement.dataset.sidebar === 'collapsed') {
        event.preventDefault();
        toggleSidebarCollapsed(false);
    }
}

function toggleSidebar(show) {
    const menuToggle = document.getElementById('menu-toggle');
    if (!menuToggle) return;
    const open = show === undefined ? !document.body.classList.contains('nav-open') : show;
    document.body.classList.toggle('nav-open', open);
    menuToggle.setAttribute('aria-expanded', String(open));
    document.getElementById('sidebar-backdrop').hidden = !open;
    if (!window.matchMedia('(max-width: 900px)').matches) return;
    const sidebar = document.getElementById('sidebar');
    if (open) {
        sidebar?.querySelector('.side-nav a[href]:not([hidden])')?.focus();
    } else if (sidebar?.contains(document.activeElement)) {
        menuToggle.focus();
    }
}

function dispatchWorkspaceAction(element, eventType, event) {
    if (element.dataset[`${eventType}SelfOnly`] === 'true' && event.target !== element) return;
    const actionName = element.dataset[`${eventType}Action`];
    const action = globalThis[actionName];
    if (typeof action !== 'function') {
        throw new Error(`Workspace action "${actionName}" is not available.`);
    }

    const argument = element.dataset[`${eventType}Argument`];
    if (argument === 'event') {
        action(event);
    } else if (argument === 'value') {
        action(element.value);
    } else if (argument !== undefined) {
        const type = element.dataset[`${eventType}ArgumentType`];
        action(type === 'boolean' ? argument === 'true' : type === 'number' ? Number(argument) : argument);
    } else {
        action();
    }
}

function findWorkspaceActionElement(event, selector) {
    const target = event.target;
    return target && typeof target.closest === 'function' ? target.closest(selector) : null;
}

function addCustomBizTypeOnEnter(event) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    globalThis.addCustomBizType();
}

document.addEventListener('click', event => {
    const element = findWorkspaceActionElement(event, '[data-click-action]');
    if (element) dispatchWorkspaceAction(element, 'click', event);
});
document.addEventListener('input', event => {
    const element = findWorkspaceActionElement(event, '[data-input-action]');
    if (element) dispatchWorkspaceAction(element, 'input', event);
});
document.addEventListener('change', event => {
    const element = findWorkspaceActionElement(event, '[data-change-action]');
    if (element) dispatchWorkspaceAction(element, 'change', event);
    if (event.target.id === 'theme-preference') setThemePreference(event.target.value);
});
document.addEventListener('submit', event => {
    const element = findWorkspaceActionElement(event, 'form[data-submit-action]');
    if (element) dispatchWorkspaceAction(element, 'submit', event);
});

document.addEventListener('DOMContentLoaded', () => {
    setThemePreference(document.documentElement.dataset.themePreference, false);
    if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
        document.querySelector('[data-autofocus]')?.focus();
    }
    const isCollapsed = document.documentElement.dataset.sidebar === 'collapsed';
    const collapseBtn = document.getElementById('sidebar-collapse');
    if (collapseBtn) {
        collapseBtn.setAttribute('aria-expanded', String(!isCollapsed));
        const label = isCollapsed ? 'Expand sidebar' : 'Collapse sidebar';
        collapseBtn.setAttribute('aria-label', label);
        collapseBtn.setAttribute('title', label);
    }
});
document.addEventListener('keydown', event => {
    const element = findWorkspaceActionElement(event, '[data-keydown-action]');
    if (element) dispatchWorkspaceAction(element, 'keydown', event);
    const modalHandled = handleAccessibleModalKeydown(event);
    if (event.key === 'Escape') {
        if (!modalHandled) {
            toggleSidebar(false);
            if (typeof toggleDiscoveryModal === 'function' && document.body.dataset.page !== 'discovery') {
                toggleDiscoveryModal(false);
            }
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
        const [resource, options] = args;
        if (typeof resource === 'string' && resource.startsWith('/api/')
            && options && options.method && options.method.toUpperCase() !== 'GET') {
            const token = document.querySelector('meta[name="csrf-token"]');
            if (token) args[1] = {...options, headers: {...options.headers, 'X-CSRF-Token': token.content}};
        }
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

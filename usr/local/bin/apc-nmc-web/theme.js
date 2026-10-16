/*
 * theme.js - pre-paint theme selection.
 *
 * Inlined into <head> BEFORE the nmc.css <style> block by page() and
 * login_page() in apc-nmc-web.py.  It must run before the stylesheet is
 * applied so the correct [data-theme] variable set is active on the very
 * first paint - otherwise the page would flash the wrong theme.
 *
 * Resolution order:
 *   1. the user's saved choice in localStorage.nmcTheme, or
 *   2. the OS/browser prefers-color-scheme media query.
 *
 * The result is written to <html data-theme="dark|light">; nmc.css
 * scopes all of its colour variables to that attribute.
 */
(function () {
    var t = localStorage.nmcTheme;
    if (!t) {
        t = matchMedia('(prefers-color-scheme:dark)').matches
            ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', t);
})();

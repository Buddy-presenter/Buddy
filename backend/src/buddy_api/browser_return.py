"""A completed browser document that hands only a one-use code to the native app."""

import base64
import hashlib

from fastapi import APIRouter
from starlette.responses import HTMLResponse

from buddy_identity.config import BROWSER_CALLBACK_PATH, BROWSER_SIGNOUT_PATH

# No request values are interpolated into HTML or JavaScript. OAuth parameters are
# read in the browser only, removed from history immediately, and never sent onwards
# to another HTTP endpoint. The native broker still enforces state and single use.
SCRIPT = r"""(() => {
  const search = window.location.search;
  const fragment = window.location.hash;
  const signedOut = window.location.pathname.endsWith('/signed-out');
  window.history.replaceState(null, '', window.location.pathname);
  const title = document.getElementById('title');
  const message = document.getElementById('message');
  const open = document.getElementById('open-buddy');
  const invalid = () => {
    title.textContent = 'Start sign-in from Buddy';
    message.textContent = 'This browser return is missing or expired. Open Buddy and try again.';
    open.hidden = true;
    open.removeAttribute('href');
  };
  if (fragment || search.length > 8192) { invalid(); return; }
  const params = new URLSearchParams(search);
  let target;
  if (signedOut) {
    if (Array.from(params).length) { invalid(); return; }
    target = 'org.buddy.desktop://oauth/signed-out';
    title.textContent = 'Return to Buddy';
    message.textContent = 'Browser sign-out is complete. Return to Buddy and close this tab.';
  } else {
    const allowed = new Set(['state', 'code', 'error', 'error_description']);
    const bad = Array.from(params.keys()).some(key =>
      !allowed.has(key) || params.getAll(key).length !== 1);
    if (bad) {
      invalid(); return;
    }
    const state = params.get('state');
    const code = params.get('code');
    const error = params.get('error');
    const safe = (value, limit) => value && value.length <= limit &&
      !/[\u0000-\u0020\u007f]/.test(value);
    if (!safe(state, 256) || params.has('code') === params.has('error') ||
        (params.has('code') && !safe(code, 4096)) ||
        (params.has('error') && !safe(error, 128))) { invalid(); return; }
    const native = new URLSearchParams({state});
    native.set(code ? 'code' : 'error', code || error);
    target = 'org.buddy.desktop://oauth/callback?' + native.toString();
    if (target.length > 8192) { invalid(); return; }
    title.textContent = error ? 'Sign-in was not completed' : 'Return to Buddy';
    message.textContent = error
      ? 'Continue in Buddy to retry or cancel. You can close this tab.'
      : 'The browser step is complete. Return to Buddy and close this tab. ' +
        'Buddy will finish checking your account.';
  }
  open.href = target;
  open.hidden = false;
  // Commit a real HTTPS page before asking the browser to launch another app.
  // Browsers that require a user gesture can use the visible fallback button.
  window.addEventListener('load', () => { window.location.assign(target); }, {once: true});
  window.addEventListener('pageshow', event => { if (event.persisted) invalid(); });
})();"""

STYLE = """html {color-scheme: dark; background: #06090D; color: #E5E5E7;
font: 17px/1.6 system-ui, sans-serif;} body {margin: 0; min-height: 100svh;
display: grid; place-items: center;} main {max-width: 34rem; margin: 2rem; padding: 2.5rem;
border: 1px solid #34343B; border-radius: 20px; background: #0E1019;}
.brand {color: #F6A142; font-weight: 700; letter-spacing: .12em; font-size: .8rem;}
h1 {font-size: 2rem; line-height: 1.2; letter-spacing: -.02em; margin: 1rem 0;}
p {color: #B5B8BA;} a {display: inline-block; padding: .8rem 1.3rem; border-radius: 10px;
background: #B45032; color: white; text-decoration: none; font-weight: 600;}
a:hover {background: #CA5938;} a:focus-visible {outline: 3px solid #F6A142; outline-offset: 5px;}
[hidden] {display: none;} .hint {font-size: .85rem; margin-top: 1.5rem;}"""


def csp_hash(content: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(content.encode()).digest()).decode() + "'"


HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="referrer" content="no-referrer"><title>Return to Buddy</title>'
    f"<style>{STYLE}</style></head><body><main>"
    '<div class="brand">BUDDY</div><h1 id="title">Return to Buddy</h1>'
    '<p id="message" role="status">Use Buddy to complete your account check.</p>'
    '<a id="open-buddy" hidden>Open Buddy</a>'
    '<p class="hint">Your browser may ask permission to open the app.</p>'
    "<noscript><p>JavaScript is needed to return to the app. Enable it, then start sign-in "
    "again from Buddy.</p></noscript></main>"
    f"<script>{SCRIPT}</script></body></html>"
)


def browser_return_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.api_route(BROWSER_CALLBACK_PATH, methods=["GET", "HEAD"])
    @router.api_route(BROWSER_SIGNOUT_PATH, methods=["GET", "HEAD"])
    async def browser_return() -> HTMLResponse:
        return HTMLResponse(
            HTML,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Cross-Origin-Opener-Policy": "same-origin",
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src {csp_hash(SCRIPT)}; "
                    f"style-src {csp_hash(STYLE)}; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"
                ),
            },
        )

    return router

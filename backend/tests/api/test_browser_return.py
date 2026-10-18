"""Browser return documents do not expose a session or trust OAuth query values."""

import json
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from buddy_api.browser_return import SCRIPT, STYLE, csp_hash
from buddy_api.middleware import RequestContextMiddleware
from buddy_identity.config import BROWSER_CALLBACK_PATH, BROWSER_SIGNOUT_PATH


@pytest.mark.parametrize("path", [BROWSER_CALLBACK_PATH, BROWSER_SIGNOUT_PATH])
def test_finish_page_is_public_fixed_and_uncacheable(api_client, path):
    plain = api_client.get(path)
    with_code = api_client.get(path + "?code=synthetic-private-code&state=synthetic-state")
    assert plain.status_code == with_code.status_code == 200
    assert plain.content == with_code.content
    assert "synthetic-private-code" not in with_code.text
    assert "<a " not in plain.text and "<button" not in plain.text
    assert 'id="open-buddy"' not in plain.text
    assert plain.headers["content-type"].startswith("text/html")
    assert plain.headers["cache-control"] == "no-store"
    assert plain.headers["referrer-policy"] == "no-referrer"
    assert plain.headers["x-frame-options"] == "DENY"
    policy = plain.headers["content-security-policy"]
    assert csp_hash(SCRIPT) in policy and csp_hash(STYLE) in policy
    assert "'unsafe-inline'" not in policy and "default-src 'none'" in policy
    assert api_client.post(path).status_code == 405
    assert api_client.head(path).status_code == 200
    assert path not in api_client.get("/openapi.json").json()["paths"]


def test_callback_query_is_removed_from_access_log_scope():
    import asyncio

    scope = {
        "type": "http",
        "path": BROWSER_CALLBACK_PATH,
        "query_string": b"code=synthetic-private-code&state=synthetic-state",
        "headers": [(b"host", b"testserver")],
    }
    observed = []

    async def app(scope, receive, send):
        observed.append(scope["query_string"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    asyncio.run(RequestContextMiddleware(app, ("testserver",))(scope, receive, send))
    assert observed == [b""] and scope["query_string"] == b""


def run_browser(query, *, signed_out=False, fragment="", script=SCRIPT):
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript behavior checks need Node.js, available on repository CI runners.")
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = Object.fromEntries(['title','message'].map(key => [key, {
  hidden: true, textContent: '', removeAttribute(key) { delete this[key]; }
}]));
const events = {};
const navigations = [];
const history = [];
const window = {
  location: { search: data.query, hash: data.fragment,
    pathname: data.signed_out ? '/auth/desktop/signed-out' : '/auth/desktop/callback',
    assign: target => navigations.push(target) },
  history: { replaceState: (...args) => history.push(args[2]) },
  addEventListener: (event, callback) => { events[event] = callback; }
};
const context = vm.createContext({window, URLSearchParams, events,
  document: { getElementById: key => nodes[key] }});
vm.runInContext(data.script, context, {timeout: 1000});
const beforeLoad = [...navigations];
vm.runInContext('if (events.load) events.load();', context, {timeout: 1000});
process.stdout.write(JSON.stringify({nodes, history, beforeLoad, navigations}));
"""
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps(
            {"script": script, "query": query, "signed_out": signed_out, "fragment": fragment}
        ),
        text=True,
        capture_output=True,
        # Hosted Windows can take several seconds to cold-start Node. Bound the
        # actual script separately above instead of timing startup as page work.
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "script",
    [
        "while (true) {}",
        "window.addEventListener('load', () => { while (true) {} });",
    ],
    ids=["initial-evaluation", "load-handler"],
)
def test_browser_script_hangs_hit_vm_limit_in_each_phase(script):
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_browser("", script=script)
    # A host-context callback would instead hit subprocess.TimeoutExpired after
    # 30 seconds. Require Node's VM error, without a fragile wall-clock assertion.
    assert "ERR_SCRIPT_EXECUTION_TIMEOUT" in error.value.stderr


def test_real_script_scrubs_url_then_opens_only_native_app_after_document_load():
    result = run_browser("?state=synthetic-state&code=one%2Buse%2Fcode")
    assert result["history"] == [BROWSER_CALLBACK_PATH]
    assert result["beforeLoad"] == []
    target = result["navigations"][0]
    assert target.startswith("org.buddy.desktop://oauth/callback?")
    assert parse_qs(urlsplit(target).query) == {
        "state": ["synthetic-state"],
        "code": ["one+use/code"],
    }
    assert result["navigations"] == [target]
    assert "Return to Buddy" in result["nodes"]["message"]["textContent"]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?state=x",
        "?code=x",
        "?code=&state=x",
        "?state=x&code=x&code=y",
        "?state=x&state=y&code=x",
        "?state=x&code=x&error=denied",
        "?state=x&error=",
        "?state=x&access_token=private",
        "?state=x&code=x&next=https://evil.test",
        "?state=x&code=%00",
        "?state=%0A&code=x",
        "?state=x&code=" + "x" * 8192,
    ],
)
def test_ambiguous_or_malicious_returns_never_launch_an_app(query):
    result = run_browser(query)
    assert result["navigations"] == []
    assert "try again" in result["nodes"]["message"]["textContent"]


def test_error_return_and_signout_keep_fixed_native_targets():
    result = run_browser("?state=synthetic-state&error=access_denied&error_description=private")
    target = result["navigations"][0]
    assert "private" not in target
    assert parse_qs(urlsplit(target).query)["error"] == ["access_denied"]
    assert run_browser("", signed_out=True)["navigations"] == [
        "org.buddy.desktop://oauth/signed-out"
    ]
    assert run_browser("?code=x", signed_out=True)["navigations"] == []
    assert run_browser("?state=x&code=y", fragment="#access_token=private")["navigations"] == []

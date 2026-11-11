"""Profile chooser, cache boundaries, and draft resets against an isolated API fixture."""

import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
FIXTURE = PACKAGE / "tests/profile-fixture"
VITE = next((candidate for candidate in (
    PACKAGE / "node_modules/.bin/vite", ROOT / "node_modules/.bin/vite"
) if candidate.exists()), None)
if VITE is None:
    raise RuntimeError("Install workspace npm dependencies before running profile_browser.py")


profiles = [
    {"id": "default", "label": "Default", "port": 8000, "is_default": True},
    {"id": "secondary", "label": "Secondary", "port": 8001, "is_default": False},
]
requests = []


def api(route):
    request = route.request
    path = urlparse(request.url).path.removeprefix("/api")
    method = request.method
    headers = request.headers
    requests.append((method, path, headers.get("x-mcp-profile"), request.post_data))
    if path == "/auth/login":
        assert "x-mcp-profile" not in headers
        body = {"token": "fixture-admin"}
    elif path == "/profiles":
        assert "x-mcp-profile" not in headers
        if method == "GET":
            body = {"profiles": profiles}
        else:
            label = json.loads(request.post_data)["label"]
            created = {"id": "created", "label": label, "port": 8002, "is_default": False}
            profiles.append(created)
            body = created
    elif path.startswith("/profiles/"):
        assert "x-mcp-profile" not in headers
        profile_id = unquote(path.split("/", 2)[2])
        profile = next(item for item in profiles if item["id"] == profile_id)
        if method == "PATCH":
            profile["label"] = json.loads(request.post_data)["label"]
            body = profile
        else:
            assert method == "DELETE" and profile_id != "default"
            profiles.remove(profile)
            route.fulfill(status=204, body="")
            return
    else:
        profile_id = headers.get("x-mcp-profile")
        assert profile_id in {item["id"] for item in profiles}, (path, profile_id)
        assert headers.get("authorization") == "Bearer fixture-admin"
        if path == "/tokens":
            body = {"state": "active", "token_set": True,
                    "configuration": {"revision": "r1", "server_time_ms": 1793328000000}}
        elif path == "/settings/mcp_auth_token/reveal":
            body = {"value": f"{profile_id}-secret"}
        elif path == "/settings":
            profile_port = next(item["port"] for item in profiles if item["id"] == profile_id)
            body = {"mcp_auth_token_masked": f"{profile_id}-masked", "mcp_auth_token_set": True,
                    "admin_password_masked": "global-masked", "editable_env_keys": [],
                    "mcp_server": {"command": "mcp", "args": [], "port": profile_port, "env": {}},
                    "proxy": {"timeout": 86400},
                    "configuration": {"revision": "r1", "server_time_ms": 1793328000000}}
        elif path == "/settings/mcp/status":
            body = {"running": True, "configuration": {"revision": "r1", "server_time_ms": 1793328000000}}
        elif path == "/config":
            body = {"endpoint": f"https://{profile_id}.example", "configuration": {"revision": "r1"}}
        elif path == "/tools":
            body = {"tools": [{"name": f"{profile_id}-tool", "enabled": True}], "disabled_tools": []}
        elif path == "/capabilities":
            body = {"backend": {"can_edit": True}, "capabilities": {}, "tools": {}}
        elif path == "/health":
            body = {"running": True}
        else:
            raise AssertionError((method, path))
    route.fulfill(status=200, json=body)


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(
    [str(VITE), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
    cwd=FIXTURE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
try:
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError(f"Vite stopped: {server.stdout.read()}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/", timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Vite did not start")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.add_init_script("localStorage.setItem('mcp-admin-token', 'fixture-admin')")
        context.route("**/api/**", api)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        origin = f"http://127.0.0.1:{port}"
        chooser = page.locator("#mcp-profile-select")

        page.goto(origin + "/tokens")
        expect(page.get_by_text("default-masked", exact=True)).to_be_visible()
        page.get_by_role("button", name="Show token", exact=True).click()
        expect(page.get_by_text("default-secret", exact=True)).to_be_visible()
        chooser.select_option("secondary")
        expect(page.get_by_text("secondary-masked", exact=True)).to_be_visible()
        assert "default-secret" not in page.locator("body").inner_text()
        assert "secondary-secret" not in page.locator("body").inner_text()
        chooser.select_option("default")
        expect(page.get_by_text("default-masked", exact=True)).to_be_visible()
        assert "default-secret" not in page.locator("body").inner_text()

        page.goto(origin + "/config")
        field = page.locator("fieldset input[type=text]")
        expect(field).to_have_value("https://default.example")
        field.fill("unsaved-default-draft")
        chooser.select_option("secondary")
        expect(field).to_have_value("https://secondary.example")
        chooser.select_option("default")
        expect(field).to_have_value("https://default.example")
        assert not any(method == "PUT" for method, *_ in requests)

        page.goto(origin + "/tools")
        expect(page.get_by_text("default-tool", exact=True)).to_be_visible()
        chooser.select_option("secondary")
        expect(page.get_by_text("secondary-tool", exact=True)).to_be_visible()
        assert "default-tool" not in page.locator("body").inner_text()
        chooser.select_option("default")
        expect(page.get_by_text("default-tool", exact=True)).to_be_visible()
        assert "secondary-tool" not in page.locator("body").inner_text()

        page.goto(origin + "/settings")
        expect(page.get_by_text("Global Admin Password")).to_be_visible()
        expect(page.get_by_text("Listening on 127.0.0.1:8000.", exact=False)).to_be_visible()
        page.get_by_placeholder("Minimum 4 characters").fill("draft-password")
        chooser.select_option("secondary")
        expect(page.get_by_text("Global Admin Password")).not_to_be_visible()
        expect(page.get_by_role("button", name="Update Password")).not_to_be_visible()
        expect(page.get_by_text("Listening on 127.0.0.1:8001.", exact=False)).to_be_visible()
        chooser.select_option("default")
        expect(page.get_by_placeholder("Minimum 4 characters")).to_have_value("")
        expect(page.get_by_text("Listening on 127.0.0.1:8000.", exact=False)).to_be_visible()
        assert not any(path == "/settings/admin_password" for _, path, *_ in requests)

        expect(page.get_by_role("button", name="Delete", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Rename", exact=True)).to_be_disabled()
        page.get_by_role("button", name="Create", exact=True).click()
        page.get_by_role("textbox", name="Profile label").fill("Worker")
        page.get_by_role("button", name="Create profile").click()
        expect(chooser).to_have_value("created")
        page.get_by_role("button", name="Rename", exact=True).click()
        label = page.get_by_role("textbox", name="Profile label")
        expect(label).to_have_value("Worker")
        label.fill("Renamed")
        page.get_by_role("button", name="Save profile").click()
        expect(chooser.locator("option:checked")).to_have_text("Renamed")
        page.get_by_role("button", name="Delete", exact=True).click()
        expect(page.get_by_text("Delete Renamed?", exact=False)).to_be_visible()
        page.get_by_role("button", name="Confirm delete profile").click()
        expect(chooser).to_have_value("default")
        expect(chooser.locator("option[value=created]")).to_have_count(0)
        assert [(method, path) for method, path, *_ in requests if method in {"PATCH", "DELETE"} or method == "POST" and path == "/profiles"] == [
            ("POST", "/profiles"), ("PATCH", "/profiles/created"), ("DELETE", "/profiles/created")]

        chooser.select_option("secondary")
        page.reload()
        expect(chooser).to_have_value("secondary")
        page.evaluate("localStorage.removeItem('mcp-admin-token')")
        page.goto(origin + "/login")
        page.get_by_label("Password").fill("global-password")
        page.get_by_role("button", name="Sign in").click()
        expect(chooser).to_have_value("secondary")
        assert any(method == "POST" and path == "/auth/login" and profile is None
                   for method, path, profile, _ in requests)
        assert not errors, errors
        browser.close()
        print(json.dumps({"checks": ["token hidden on switch", "drafts discarded", "tools isolated",
                                  "global password and login", "profile CRUD", "selection survives reload"],
                          "profile_requests": len([item for item in requests if item[1].startswith("/profiles")])}))
finally:
    server.terminate()
    try:
        server.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.communicate()

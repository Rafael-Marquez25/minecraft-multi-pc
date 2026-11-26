import hashlib
import io
import socket
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Network

import httpx
import pytest
from fastapi.testclient import TestClient

from mcmp.minecraft import Minecraft, MinecraftError
from mcmp.protocol import CONSOLE_PAGE, OFFLINE_AFTER, Job, token_digest
from mcmp.snapshots import InvalidSnapshot, create_archive, inspect_archive
from mcmp.web import create_app

NETWORK = IPv4Network("127.0.0.0/8")
API = "/api/v1"
PASSWORD = "a long test password"
USER_PASSWORD = "a different test password"


@pytest.fixture
def panel(tmp_path):
    app = create_app(tmp_path / "coordinator", NETWORK)
    app.state.store.add_account("admin", PASSWORD)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client)
        yield app, client


def sign_in(client, name="admin", password=PASSWORD):
    assert client.post(f"{API}/login", json={"name": name, "password": password}).status_code == 200
    client.headers["X-CSRF-Token"] = client.get(f"{API}/status").json()["csrf"]


def configure(client):
    settings = client.get(f"{API}/status").json()["settings"]
    settings.update(eula=True, seed="mundo de prueba", memory_mib=1024)
    assert client.put(f"{API}/settings", json=settings).status_code == 200
    return settings


def enroll(client, name, address):
    code = client.post(f"{API}/enrollment-code").json()["code"]
    response = client.post(f"{API}/agents/enroll", json={"name": name, "code": code})
    assert response.status_code == 201
    assert client.post(f"{API}/agents/enroll", json={"name": name, "code": code}).status_code == 400
    agent = response.json()
    auth = {"Authorization": f'Bearer {agent["token"]}'}
    assert client.post(f"{API}/agents/poll", headers=auth).status_code == 200
    assert client.put(f'{API}/agents/{agent["id"]}/address', json={"address": address}).status_code == 200
    pairing = client.post(f"{API}/agents/pairing-code", headers=auth).json()
    assert client.post(f"{API}/pair", json=pairing).status_code == 200
    assert client.post(f"{API}/pair", json=pairing).status_code == 400
    return agent["id"], auth


def event(client, run, auth, name):
    return client.post(f"{API}/runs/{run}/events", json={"event": name}, headers=auth)


def make_world(tmp_path):
    folder = tmp_path / "minecraft"
    (folder / "world").mkdir(parents=True)
    (folder / "world" / "level.dat").write_bytes(b"a world")
    (folder / "world" / "session.lock").touch()
    archive = tmp_path / "world.tar.gz"
    create_archive(folder, archive)
    return archive.read_bytes()


def upload_headers(content, auth):
    return {**auth, "X-World-SHA256": hashlib.sha256(content).hexdigest()}


def test_dashboard_login_cookies_and_csrf(panel):
    app, client = panel
    assert "Servidor de Minecraft" in client.get("/").text
    assert client.get("/static/panel.js").status_code == 200
    assert client.get("/static/panel.css").status_code == 200
    with TestClient(app, base_url="https://testserver") as anonymous:
        assert anonymous.get(f"{API}/status").status_code == 401
        bad = anonymous.post(
            f"{API}/login", headers={"Origin": "https://other.example"},
            json={"name": "admin", "password": PASSWORD},
        )
        assert bad.status_code == 403
        sign_in(anonymous)
        anonymous.headers.pop("X-CSRF-Token")
        assert anonymous.post(f"{API}/enrollment-code").status_code == 403


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("certificates")
    for name, is_ca in (("ca", "TRUE"), ("site", "FALSE")):
        subprocess.run(
            [
                "openssl", "req", "-new", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-noenc", "-config", "/dev/null", "-days", "1", "-subj", f"/CN={name}",
                "-addext", f"basicConstraints=critical,CA:{is_ca}",
                "-out", f"{name}.pem", "-keyout", f"{name}-key.pem",
            ],
            cwd=directory, check=True, capture_output=True,
        )
    return directory


def test_admin_and_user_can_download_ca_before_pairing(tmp_path, certificates):
    ca_path = certificates / "ca.pem"
    app = create_app(tmp_path / "coordinator", NETWORK, ca_path=ca_path)
    app.state.store.add_account("admin", PASSWORD)
    download = f"{API}/ca-certificate"
    with TestClient(app, base_url="https://testserver") as admin, TestClient(
        app, base_url="https://testserver",
    ) as user:
        assert user.get(download).status_code == 401
        sign_in(admin)
        assert admin.put(f"{API}/accounts/user", json={"password": USER_PASSWORD}).status_code == 200
        sign_in(user, "user", USER_PASSWORD)
        for client in (admin, user):
            assert client.get(f"{API}/status").json()["paired"] is None
            assert "Descargar CA del panel" in client.get("/").text
            response = client.get(download)
            assert response.status_code == 200
            assert response.headers["Content-Disposition"] == 'attachment; filename="minecraft-ca.pem"'
            assert response.headers["Cache-Control"] == "no-store"
            assert response.content == ca_path.read_bytes()


@pytest.mark.parametrize("role", ("admin", "user"))
def test_vpn_self_service_enrollment(tmp_path, certificates, role):
    vpn = IPv4Network("10.42.0.0/24")
    app = create_app(
        tmp_path / "coordinator", vpn, ca_path=certificates / "ca.pem",
        bind=IPv4Address("10.42.0.1"), port=8444,
    )
    app.state.store.add_account("admin", PASSWORD)
    with TestClient(app, base_url="https://mc.home.arpa") as browser, TestClient(
        app, base_url="https://10.42.0.1:8444", client=("10.42.0.18", 50000),
    ) as agent:
        sign_in(browser)
        configure(browser)
        if role == "user":
            assert browser.put(f"{API}/accounts/user", json={"password": USER_PASSWORD}).status_code == 200
            sign_in(browser, "user", USER_PASSWORD)
        assert 'id="device-enrollment"' in browser.get("/").text
        assert "./scripts/deploy-agent.sh --url https://10.42.0.1:8444" in browser.get("/").text
        assert browser.get(f"{API}/ca-certificate").status_code == 200

        registration = f"{API}/enrollment-code"
        options = {"automatic_address": True}
        assert agent.post(registration, json=options).status_code == 401
        assert browser.post(registration, json=options, headers={"X-CSRF-Token": ""}).status_code == 403
        assert browser.post(registration, json=options, headers={"Origin": "https://otro.example"}).status_code == 403
        if role == "user":
            assert browser.post(registration, json={"automatic_address": False}).status_code == 403
        code = browser.post(registration, json=options).raise_for_status().json()["code"]

        payload = {"code": code, "name": "PC por VPN", "address": "10.42.0.70"}
        response = agent.post(f"{API}/agents/enroll", json=payload, headers={"X-Forwarded-For": "10.42.0.80"})
        assert response.status_code == 201
        identity = response.json()
        assert agent.post(f"{API}/agents/enroll", json=payload).status_code == 400

        auth = {"Authorization": f'Bearer {identity["token"]}'}
        pairing = agent.post(f"{API}/agents/pairing-code", headers=auth).raise_for_status().json()
        assert browser.post(f"{API}/pair", json=pairing).status_code == 200
        assert browser.post(f"{API}/pair", json=pairing).status_code == 400
        assert agent.post(f"{API}/agents/poll", headers=auth).status_code == 200
        started = browser.post(f"{API}/start")
        assert started.status_code == 202
        run = started.json()
        assert run["paired"] == identity["id"]
        assert run["agents"][0]["address"] == "10.42.0.18"
        assert event(agent, run["run_id"], auth, "claim").status_code == 200
        assert event(agent, run["run_id"], auth, "ready").status_code == 200
        assert browser.get(f"{API}/status").json()["run"]["endpoint"] == "10.42.0.18:25565"


def test_vpn_enrollment_rejects_proxy_and_outside_peer_without_consuming_code(tmp_path):
    vpn = IPv4Network("10.42.0.0/24")
    app = create_app(tmp_path / "coordinator", vpn, bind=IPv4Address("10.42.0.1"))
    store = app.state.store
    store.add_account("admin", PASSWORD)
    with TestClient(app, base_url="https://mc.home.arpa") as browser:
        sign_in(browser)
        registration = f"{API}/enrollment-code"
        options = {"automatic_address": True}
        expired = browser.post(registration, json=options).raise_for_status().json()
        code = browser.post(registration, json=options).raise_for_status().json()
        with store.connection(write=True) as db:
            db.execute("UPDATE codes SET expires = 0 WHERE digest = ?", (token_digest(expired["code"]),))
        body = {**code, "name": "PC por VPN"}
        for source in ("192.0.2.4", "10.42.0.1"):
            with TestClient(app, base_url="https://10.42.0.1:8443", client=(source, 50000)) as agent:
                response = agent.post(
                    f"{API}/agents/enroll", json=body, headers={"X-Forwarded-For": "10.42.0.5"},
                )
                assert response.status_code == 400
                assert "URL HTTPS directa" in response.json()["detail"]
        assert browser.get(f"{API}/status").json()["agents"] == []
        with TestClient(app, base_url="https://10.42.0.1:8443", client=("10.42.0.5", 50000)) as agent:
            assert agent.post(f"{API}/agents/enroll", json={**expired, "name": body["name"]}).status_code == 400
            assert agent.post(f"{API}/agents/enroll", json=body).status_code == 201


@pytest.mark.parametrize("kind", ("missing", "invalid", "site", "private-key", "multiple"))
def test_unavailable_ca_hides_download(tmp_path, certificates, kind):
    ca_path = tmp_path / "ca.pem"
    valid_ca = (certificates / "ca.pem").read_bytes()
    contents = {
        "invalid": b"-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n",
        "site": (certificates / "site.pem").read_bytes(),
        "private-key": valid_ca + (certificates / "ca-key.pem").read_bytes(),
        "multiple": valid_ca * 2,
    }
    if kind != "missing":
        ca_path.write_bytes(contents[kind])
    app = create_app(tmp_path / "coordinator", NETWORK, ca_path=ca_path)
    app.state.store.add_account("admin", PASSWORD)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client)
        assert "Descargar CA del panel" not in client.get("/").text
        assert client.get(f"{API}/ca-certificate").status_code == 404


def test_user_can_play_and_read_console_but_cannot_manage_the_server(panel, tmp_path):
    app, admin = panel
    settings = configure(admin)
    admin_host, admin_auth = enroll(admin, "PC del admin", "127.0.0.2")
    code = admin.post(f"{API}/enrollment-code").json()["code"]
    agent = admin.post(f"{API}/agents/enroll", json={"code": code, "name": "PC de user"}).json()
    auth = {"Authorization": f'Bearer {agent["token"]}'}
    pairing = admin.post(f"{API}/agents/pairing-code", headers=auth).json()
    path = f"{API}/accounts/user"
    assert admin.put(path, json={"password": USER_PASSWORD}).status_code == 200
    assert admin.get(f"{API}/status").json()["user_configured"] is True

    with TestClient(app, base_url="https://testserver") as user:
        sign_in(user, "user", USER_PASSWORD)
        state = user.get(f"{API}/status").json()
        assert state["role"] == "user"
        assert state["agents"] == []
        assert state["settings"] == {"version": settings["version"], "eula": True}
        assert "network" not in state and "user_configured" not in state
        assert user.post(f"{API}/start").status_code == 409
        assert user.post(f"{API}/pair", json=pairing).status_code == 409
        address = f'{API}/agents/{agent["id"]}/address'
        assert admin.put(address, json={"address": "127.0.0.3"}).status_code == 200
        assert user.post(f"{API}/pair", json=pairing).status_code == 200
        assert user.post(f"{API}/start").status_code == 409
        poll = f"{API}/agents/poll"
        assert admin.post(poll, headers=auth).status_code == 200
        assert user.post(f"{API}/start", headers={"X-CSRF-Token": ""}).status_code == 403
        started = user.post(f"{API}/start")
        assert started.status_code == 202
        assert [host["id"] for host in started.json()["agents"]] == [agent["id"]]
        run = f'{API}/runs/{started.json()["run_id"]}'
        assert event(admin, started.json()["run_id"], auth, "claim").status_code == 200
        assert event(admin, started.json()["run_id"], auth, "ready").status_code == 200
        assert admin.post(poll, headers=auth, json={}).status_code == 200
        assert admin.put(f"{run}/console?offset=0", headers=auth, content=b"Done!").status_code == 200
        state = user.get(f"{API}/status").json()
        assert state["run"]["endpoint"] == "127.0.0.3:25565"
        assert state["console"]["commands"] is False
        assert user.get(f"{run}/console").content == b"Done!"

        command = admin.post(f"{run}/commands", json={"command": "list"})
        assert command.status_code == 202
        for method, route, body in [
            ("PUT", path, {"password": PASSWORD}),
            ("PUT", f"{API}/settings", settings),
            ("PUT", address, {"address": "127.0.0.4"}),
            ("POST", f"{API}/enrollment-code", None),
            ("POST", f"{run}/recover", {"confirmed": True}),
            ("POST", f"{run}/commands", {"command": "stop"}),
            ("GET", f'{run}/commands/{command.json()["id"]}', None),
        ]:
            assert user.request(method, route, json=body).status_code == 403, route

        assert user.post(f"{run}/stop").status_code == 202
        assert event(admin, started.json()["run_id"], auth, "saving").status_code == 200
        content = make_world(tmp_path)
        assert admin.put(f"{run}/snapshot", content=content, headers=upload_headers(content, auth)).status_code == 200
        assert user.get(f"{API}/status").json()["saved_at"] is not None
        assert user.get(f"{run}/console").content == b"Done!"

        admin_started = admin.post(f"{API}/start").json()
        assert admin_started["run"]["agent"] == admin_host
        second = admin_started["run_id"]
        assert event(admin, second, admin_auth, "claim").status_code == 200
        assert event(admin, second, admin_auth, "ready").status_code == 200
        assert user.post(f"{API}/runs/{second}/stop").status_code == 202


def test_user_password_reset_keeps_pairings_separate_and_revokes_old_sessions(panel):
    app, admin = panel
    host, auth = enroll(admin, "PC compartido", "127.0.0.2")
    path = f"{API}/accounts/user"
    assert admin.get(f"{API}/status").json()["user_configured"] is False
    assert admin.put(path, json={"password": "short"}).status_code == 422
    assert admin.put(path, json={"password": USER_PASSWORD}, headers={"X-CSRF-Token": ""}).status_code == 403
    assert admin.put(path, json={"password": USER_PASSWORD}).status_code == 200
    pairing = admin.post(f"{API}/agents/pairing-code", headers=auth).json()
    with TestClient(app, base_url="https://testserver") as other_admin:
        sign_in(other_admin)
        sign_in(admin, "user", USER_PASSWORD)
        assert admin.get(f"{API}/status").json()["paired"] is None
        assert admin.post(f"{API}/pair", json=pairing).status_code == 200
        assert admin.get(f"{API}/status").json()["paired"] == host
        with TestClient(app, base_url="https://testserver") as second_session:
            sign_in(second_session, "user", USER_PASSWORD)
            assert other_admin.put(path, json={"password": PASSWORD}).status_code == 200
            assert second_session.get(f"{API}/status").status_code == 401
        assert admin.get(f"{API}/status").status_code == 401
        assert admin.post(f"{API}/login", json={"name": "user", "password": USER_PASSWORD}).status_code == 401
        sign_in(admin)
        assert admin.get(f"{API}/status").json()["paired"] == host
        sign_in(admin, "user", PASSWORD)
        assert admin.get(f"{API}/status").json()["paired"] == host


def test_existing_database_keeps_admin_access_settings_and_world(panel, tmp_path):
    app, client = panel
    settings = configure(client)
    host, auth = enroll(client, "PC existente", "127.0.0.2")
    saved = client.post(f"{API}/start").json()["run_id"]
    assert event(client, saved, auth, "claim").status_code == 200
    assert event(client, saved, auth, "saving").status_code == 200
    content = make_world(tmp_path)
    assert client.put(
        f"{API}/runs/{saved}/snapshot", content=content, headers=upload_headers(content, auth),
    ).status_code == 200

    with app.state.store.connection(write=True) as db:
        db.execute("ALTER TABLE accounts DROP COLUMN role")
        db.execute("INSERT INTO accounts (name, password) SELECT 'user', password FROM accounts WHERE name = 'admin'")

    restarted = create_app(tmp_path / "coordinator", NETWORK)
    with TestClient(restarted, base_url="https://testserver") as browser:
        browser.cookies.update(client.cookies)
        browser.headers["X-CSRF-Token"] = client.headers["X-CSRF-Token"]
        state = browser.get(f"{API}/status").json()
        assert state["role"] == "admin"
        assert state["settings"] == settings
        assert state["paired"] == host
        assert state["saved_at"] is not None
        assert browser.put(f"{API}/accounts/user", json={"password": USER_PASSWORD}).status_code == 409
        run = browser.post(f"{API}/start").json()["run_id"]
        assert browser.get(f"{API}/runs/{run}/snapshot", headers=auth).content == content
        sign_in(browser, "user")
        assert browser.get(f"{API}/status").json()["role"] == "admin"


def test_two_browsers_concurrent_start_stop_and_restore(panel, tmp_path):
    app, one = panel
    configure(one)
    host_a, auth_a = enroll(one, "PC A", "127.0.0.2")
    with TestClient(app, base_url="https://testserver") as two:
        sign_in(two)
        host_b, auth_b = enroll(two, "PC B", "127.0.0.3")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.post, f"{API}/start") for client in (one, two)]
        starts = [future.result() for future in futures]
        assert sorted(item.status_code for item in starts) == [200, 202]
        assert starts[0].json()["run_id"] == starts[1].json()["run_id"]
        run = starts[0].json()["run_id"]
        winner = starts[0].json()["run"]["agent"]
        winner_auth, next_client, next_auth = (
            (auth_a, two, auth_b) if winner == host_a else (auth_b, one, auth_a)
        )
        assert winner in {host_a, host_b}
        assert event(one, run, next_auth, "claim").status_code == 410
        assert event(one, run, winner_auth, "claim").status_code == 200
        assert event(one, run, winner_auth, "ready").status_code == 200
        addresses = [client.get(f"{API}/status").json()["run"]["endpoint"] for client in (one, two)]
        assert addresses[0] == addresses[1]
        assert addresses[0] in {"127.0.0.2:25565", "127.0.0.3:25565"}
        assert one.post(f"{API}/runs/{run}/stop").status_code == 202
        assert event(one, run, winner_auth, "saving").status_code == 200

        content = make_world(tmp_path)
        endpoint = f"{API}/runs/{run}/snapshot"
        bad = one.put(endpoint, content=content[:20], headers=upload_headers(content, winner_auth))
        assert bad.status_code == 400
        assert one.get(f"{API}/status").json()["state"] == "saving"
        assert one.get(f"{API}/status").json()["saved_at"] is None
        headers = upload_headers(content, winner_auth)
        assert one.put(endpoint, content=content, headers=headers).status_code == 200
        assert one.put(endpoint, content=content, headers=headers).status_code == 200
        assert one.get(f"{API}/status").json()["state"] == "idle"

        next_run = next_client.post(f"{API}/start").json()["run_id"]
        assert next_run != run
        followup = next_client.get(f"{API}/runs/{next_run}/snapshot", headers=next_auth)
        assert followup.content == content


def test_restarted_coordinator_keeps_run_until_recovery(panel, tmp_path):
    app, one = panel
    configure(one)
    host, auth = enroll(one, "PC A", "127.0.0.2")
    previous = make_world(tmp_path)
    first = one.post(f"{API}/start").json()["run_id"]
    assert event(one, first, auth, "claim").status_code == 200
    assert event(one, first, auth, "saving").status_code == 200
    assert one.put(
        f"{API}/runs/{first}/snapshot", content=previous, headers=upload_headers(previous, auth)
    ).status_code == 200
    run = one.post(f"{API}/start").json()["run_id"]
    assert event(one, run, auth, "claim").status_code == 200
    assert event(one, run, auth, "ready").status_code == 200
    assert one.post(f"{API}/runs/{run}/stop").status_code == 202
    assert event(one, run, auth, "saving").status_code == 200
    corrupted = b"no world"
    assert one.put(
        f"{API}/runs/{run}/snapshot", content=corrupted, headers=upload_headers(corrupted, auth)
    ).status_code == 400
    with app.state.store.connection(write=True) as db:
        db.execute("UPDATE agents SET seen = ? WHERE id = ?", (time.time() - OFFLINE_AFTER - 1, host))

    restarted = create_app(tmp_path / "coordinator", NETWORK)
    with TestClient(restarted, base_url="https://testserver") as two:
        sign_in(two)
        _, next_auth = enroll(two, "PC B", "127.0.0.3")
        assert two.get(f"{API}/status").json()["state"] == "offline"
        assert two.post(f"{API}/start").json()["run_id"] == run
        path = f"{API}/runs/{run}/recover"
        assert two.post(path, json={"confirmed": False}).status_code == 422
        assert two.post(path, json={"confirmed": True}).status_code == 200
        upload = two.put(f"{API}/runs/{run}/snapshot", content=previous, headers=upload_headers(previous, auth))
        assert upload.status_code == 410
        started = two.post(f"{API}/start")
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        assert two.get(f"{API}/runs/{run_id}/snapshot", headers=next_auth).content == previous


def test_console_resumes_and_keeps_only_the_latest_execution(panel):
    app, client = panel
    configure(client)
    _, auth = enroll(client, "Anfitrión", "127.0.0.2")
    with TestClient(app, base_url="https://testserver") as outsider:
        sign_in(outsider)
        _, other_auth = enroll(outsider, "Otro PC", "127.0.0.3")
    run = client.post(f"{API}/start").json()["run_id"]
    endpoint = f"{API}/runs/{run}/console"
    progress = {"phase": "jar", "bytes": 512, "total": 2048}
    poll = f"{API}/agents/poll"
    client.post(poll, headers=other_auth, json={"run": run, "progress": progress})
    assert client.get(f"{API}/status").json()["progress"] is None
    client.post(poll, headers=auth, json={"run": run, "progress": progress})
    assert client.get(f"{API}/status").json()["progress"] == progress

    prefix = "Arranque 💎".encode()
    suffix = b"a" * (CONSOLE_PAGE - 2)
    first, second = (prefix + suffix)[:CONSOLE_PAGE], (prefix + suffix)[CONSOLE_PAGE:]
    assert client.put(endpoint, params={"offset": 0}, headers=other_auth, content=first).status_code == 410
    assert client.put(endpoint, params={"offset": 0}, headers=auth, content=first).json()["offset"] == len(first)
    assert client.put(endpoint, params={"offset": 0}, headers=auth, content=first).json()["offset"] == len(first)
    assert client.put(endpoint, params={"offset": len(first)}, headers=auth, content=second).json()["offset"] == len(prefix + suffix)
    tail = client.get(endpoint)
    assert tail.content == suffix
    assert client.get(endpoint, params={"before": tail.headers["X-Log-Start"]}).content == prefix
    assert event(client, run, auth, "failed").status_code == 200
    assert client.get(endpoint).content == suffix
    assert client.post(f"{API}/start").status_code == 202
    assert client.get(endpoint).status_code == 410
    assert not (app.state.store.consoles / run).exists()


def test_console_commands_use_session_and_go_to_the_host_once(panel):
    app, client = panel
    configure(client)
    _, auth = enroll(client, "Anfitrión", "127.0.0.2")
    with TestClient(app, base_url="https://testserver") as other:
        sign_in(other)
        _, other_auth = enroll(other, "Otro PC", "127.0.0.3")
    run = client.post(f"{API}/start").json()["run_id"]
    commands = f"{API}/runs/{run}/commands"
    poll = f"{API}/agents/poll"
    assert client.post(commands, json={"command": "list"}).status_code == 409
    assert event(client, run, auth, "claim").status_code == 200
    assert event(client, run, auth, "ready").status_code == 200
    assert client.post(commands, json={"command": "list"}).status_code == 409
    client.post(poll, headers=auth, json={})

    with TestClient(app, base_url="https://testserver") as browser:
        assert browser.get(f"{API}/runs/{run}/console").status_code == 401
        assert browser.post(commands, json={"command": "list"}).status_code == 401
        sign_in(browser)
        assert browser.post(commands, headers={"X-CSRF-Token": ""}, json={"command": "list"}).status_code == 403
        assert browser.post(commands, json={"command": "list\nstop"}).status_code == 422

    first = client.post(commands, json={"command": " /list "}).json()["id"]
    stop = client.post(commands, json={"command": "/minecraft:stop"}).json()["id"]
    assert client.post(poll, headers=other_auth, json={}).json()["command"] is None
    assert client.post(poll, headers=auth, json={}).json()["command"] == {"id": first, "command": "list"}
    assert client.post(poll, headers=auth, json={}).json()["command"] is None
    assert client.get(f"{commands}/{first}").json()["status"] == "pending"
    reply = client.post(poll, headers=auth, json={"written": first}).json()
    assert reply["job"]["state"] == "stopping"
    assert reply["command"] is None
    assert client.get(f"{commands}/{first}").json()["status"] == "written"
    assert client.get(f"{commands}/{stop}").json()["status"] == "written"
    assert client.post(commands, json={"command": "list"}).status_code == 409
    assert event(client, run, auth, "interrupted").status_code == 200
    assert client.post(f"{API}/runs/{run}/recover", json={"confirmed": True}).status_code == 200
    assert client.post(commands, json={"command": "list"}).status_code == 410


def test_archive_blocks_unsafe_path_and_checks_gzip_footer(tmp_path):
    malicious = tmp_path / "malicious.tar.gz"
    with tarfile.open(malicious, "w:gz") as archive:
        member = tarfile.TarInfo("world/../../escaped")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(InvalidSnapshot):
        inspect_archive(malicious)

    content = make_world(tmp_path)
    archive = tmp_path / "broken.tar.gz"
    archive.write_bytes(content[:-5])
    with pytest.raises(InvalidSnapshot):
        inspect_archive(archive)


@pytest.mark.parametrize("total", [5, None])
def test_jar_progress_reports_streamed_bytes(tmp_path, monkeypatch, total):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ab"
            yield b"cde"

    @contextmanager
    def stream(method, url, **kwargs):
        headers = {"Content-Length": str(total)} if total else {}
        yield httpx.Response(200, request=httpx.Request(method, url), headers=headers, stream=Chunks())

    monkeypatch.setattr("mcmp.minecraft.httpx.stream", stream)
    monkeypatch.setattr("mcmp.minecraft.SERVER_SHA1", hashlib.sha1(b"abcde").hexdigest())
    minecraft = Minecraft("java", tmp_path)
    updates = []
    minecraft.prepare_jar(lambda size, length: updates.append((size, length)))
    assert updates == [(0, None), (0, total), (2, total), (5, total)]
    assert minecraft.jar.read_bytes() == b"abcde"


def test_agent_rejects_incompatible_java(tmp_path):
    old_java = tmp_path / "old-java"
    old_java.write_text('#!/bin/sh\nprintf \'openjdk version "21.0.1"\\n\' >&2\n')
    old_java.chmod(0o700)
    job = Job(
        id="00000000-0000-0000-0000-000000000001", state="starting", address="127.0.0.1",
        settings={"eula": True}, snapshot=None,
    )
    with pytest.raises(MinecraftError, match="requiere Java 25"):
        Minecraft(str(old_java), tmp_path).preflight(job)



def test_agent_rejects_occupied_port(tmp_path):
    java = tmp_path / "java"
    java.write_text('#!/bin/sh\nprintf \'openjdk version "25.0.1"\\n\' >&2\n')
    java.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        job = Job(
            id="00000000-0000-0000-0000-000000000001", state="starting", address="127.0.0.1",
            settings={"eula": True, "port": listener.getsockname()[1]}, snapshot=None,
        )
        with pytest.raises(MinecraftError, match="puerto está ocupado"):
            Minecraft(str(java), tmp_path).preflight(job)

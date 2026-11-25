import hashlib
import io
import socket
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ipaddress import IPv4Network

import httpx
import pytest
from fastapi.testclient import TestClient

from mcmp.minecraft import Minecraft, MinecraftError
from mcmp.protocol import CONSOLE_PAGE, OFFLINE_AFTER, Job
from mcmp.snapshots import InvalidSnapshot, create_archive, inspect_archive
from mcmp.web import create_app

NETWORK = IPv4Network("127.0.0.0/8")
API = "/api/v1"
PASSWORD = "a long test password"


@pytest.fixture
def panel(tmp_path):
    app = create_app(tmp_path / "coordinator", NETWORK)
    app.state.store.add_account("admin", PASSWORD)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client)
        yield app, client


def sign_in(client):
    assert client.post(f"{API}/login", json={"name": "admin", "password": PASSWORD}).status_code == 200
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

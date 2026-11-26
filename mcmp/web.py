import hashlib
import hmac
import os
import re
import ssl
import tempfile
import time
from collections import defaultdict, deque
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID

from fastapi import Cookie, Depends, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .protocol import API_PREFIX, CONSOLE_PAGE, MAX_SNAPSHOT_BYTES, AgentReport, Settings
from .snapshots import InvalidSnapshot, inspect_archive
from .store import AUTOMATIC_ENROLLMENT, BROWSER_AGE, SESSION_AGE, Problem, Store

HERE = Path(__file__).parent
SESSION_COOKIE = "__Host-mcmp-session"
BROWSER_COOKIE = "__Host-mcmp-browser"
USER_BROWSER_COOKIE = "__Host-mcmp-user-browser"


class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserPassword(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class Code(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class EnrollmentCodeRequest(BaseModel):
    automatic_address: bool = False


class Enrollment(Code):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")


class Address(BaseModel):
    address: IPv4Address


class Event(BaseModel):
    event: Literal["claim", "ready", "saving", "failed", "interrupted"]
    reason: str | None = Field(default=None, max_length=300)


class Recovery(BaseModel):
    confirmed: Literal[True]


class Command(BaseModel):
    command: str = Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x1f\x7f\x85\u2028\u2029]+$")


def set_cookie(response: Response, name: str, value: str, age: int) -> None:
    response.set_cookie(name, value, max_age=age, secure=True, httponly=True, samesite="strict")


def load_ca_certificate(path: Path) -> bytes | None:
    try:
        pem = path.read_bytes().strip()
        if not re.fullmatch(
            rb"-----BEGIN CERTIFICATE-----\r?\n"
            rb"[A-Za-z0-9+/=\r\n]+\r?\n"
            rb"-----END CERTIFICATE-----",
            pem,
        ):
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem.decode("ascii"))
    except (OSError, ValueError):
        return None
    authorities = context.get_ca_certs(binary_form=True)
    if len(authorities) != 1:
        return None
    return ssl.DER_cert_to_PEM_cert(authorities[0]).encode("ascii")


def create_app(
    directory: Path, network: IPv4Network, *, ca_path: Path | None = None,
    bind: IPv4Address | None = None, port: int = 8443,
) -> FastAPI:
    store = Store(directory, network)
    ca_certificate = load_ca_certificate(ca_path) if ca_path is not None else None
    agent_url = f"https://{bind}:{port}" if bind is not None else None
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    templates = Jinja2Templates(directory=HERE / "templates")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    login_attempts = defaultdict(deque)
    login_lock = Lock()
    upload_lock = Lock()
    upload_run = None
    upload_activity = None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Problem)
    async def handle_problem(request: Request, error: Problem):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, error: RequestValidationError):
        labels = {
            "name": "usuario", "password": "contraseña", "address": "IPv4", "code": "código",
            "memory_mib": "RAM", "port": "puerto", "seed": "semilla", "max_players": "jugadores",
            "command": "comando",
            "difficulty": "dificultad",
        }
        fields = list(dict.fromkeys(labels.get(item["loc"][-1], "solicitud") for item in error.errors()))
        return JSONResponse({"detail": f'Revisa los campos: {", ".join(fields)}.'}, status_code=422)

    def browser_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise Problem("Origen del navegador no permitido.", 403)

    def session(key: str = Cookie(default="", alias=SESSION_COOKIE)):
        row = store.session(key)
        if row is None:
            raise Problem("Inicia sesión en el panel.", 401)
        return row

    def edit(request: Request, row=Depends(session)):
        browser_origin(request)
        csrf = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf.encode(), row["csrf"].encode()):
            raise Problem("La sesión no es válida para esta acción.", 403)
        return row

    def admin_session(row=Depends(session)):
        if row["role"] != "admin":
            raise Problem("Esta acción requiere una cuenta de administración.", 403)
        return row

    def admin_edit(row=Depends(edit)):
        return admin_session(row)

    def browser_cookie(row) -> str:
        return BROWSER_COOKIE if row["role"] == "admin" else USER_BROWSER_COOKIE

    def agent(request: Request):
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not key:
            raise Problem("Falta la credencial del agente.", 401)
        return store.authenticate_agent(key)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"ca_available": ca_certificate is not None, "agent_url": agent_url},
        )

    @app.get(f"{API_PREFIX}/ca-certificate")
    def download_ca_certificate(row=Depends(session)):
        if ca_certificate is None:
            raise Problem("La CA del panel no está disponible.", 404)
        return Response(
            ca_certificate, media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="minecraft-ca.pem"'},
        )

    @app.post(f"{API_PREFIX}/login")
    def login(request: Request, response: Response, credentials: Credentials):
        browser_origin(request)
        if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise Problem("El panel espera una solicitud JSON.", 415)
        client = request.client.host if request.client else ""
        with login_lock:
            now = time.monotonic()
            for key in list(login_attempts):
                history = login_attempts[key]
                while history and now - history[0] >= 60:
                    history.popleft()
                if not history:
                    del login_attempts[key]
            history = login_attempts[client]
            if len(history) >= 10:
                raise Problem("Demasiados intentos. Espera un minuto y vuelve a probar.", 429)
            history.append(now)
        key = store.login(credentials.name, credentials.password)
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            store.logout(old)
        set_cookie(response, SESSION_COOKIE, key, SESSION_AGE)
        return {"ok": True}

    @app.post(f"{API_PREFIX}/logout")
    def logout(request: Request, response: Response, row=Depends(edit)):
        store.logout(request.cookies[SESSION_COOKIE])
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get(f"{API_PREFIX}/status")
    def status(request: Request, row=Depends(session)):
        overview = store.overview(
            row["account"], request.cookies.get(browser_cookie(row), ""), admin=row["role"] == "admin",
        )
        if overview["state"] == "saving":
            with upload_lock:
                if overview["run"]["id"] == upload_run:
                    overview["progress"] = upload_activity.copy()
        return {**overview, "csrf": row["csrf"], "user": row["name"], "role": row["role"]}

    @app.put(f"{API_PREFIX}/accounts/user")
    def user_password(values: UserPassword, row=Depends(admin_edit)):
        store.set_user_password(values.password)
        return {"ok": True}

    @app.put(f"{API_PREFIX}/settings")
    def settings(values: Settings, row=Depends(admin_edit)):
        store.update_settings(values)
        return {"ok": True}

    @app.put(f"{API_PREFIX}/agents/{{agent_id}}/address")
    def address(agent_id: UUID, values: Address, row=Depends(admin_edit)):
        store.set_address(str(agent_id), str(values.address))
        return {"ok": True}

    @app.post(f"{API_PREFIX}/enrollment-code")
    def enrollment_code(values: EnrollmentCodeRequest | None = None, row=Depends(edit)):
        if values is not None and values.automatic_address:
            return {"code": store.create_code(AUTOMATIC_ENROLLMENT)}
        admin_session(row)
        return {"code": store.create_code("enroll")}

    @app.post(f"{API_PREFIX}/pair")
    def pair(request: Request, response: Response, values: Code, row=Depends(edit)):
        cookie = browser_cookie(row)
        browser = store.pair(
            row["account"], request.cookies.get(cookie, ""), values.code, require_configured=row["role"] == "user",
        )
        set_cookie(response, cookie, browser, BROWSER_AGE)
        return {"ok": True}

    @app.post(f"{API_PREFIX}/start")
    def start(request: Request, response: Response, row=Depends(edit)):
        browser = request.cookies.get(browser_cookie(row), "")
        run, created = store.start(row["account"], browser)
        response.status_code = 202 if created else 200
        return {
            **store.overview(row["account"], browser, admin=row["role"] == "admin"),
            "run_id": run, "created": created,
        }

    @app.post(f"{API_PREFIX}/runs/{{run_id}}/stop", status_code=202)
    def stop(run_id: UUID, row=Depends(edit)):
        store.stop(str(run_id))
        return {"ok": True}

    @app.post(f"{API_PREFIX}/runs/{{run_id}}/recover")
    def recover(run_id: UUID, values: Recovery, row=Depends(admin_edit)):
        store.recover(str(run_id))
        return {"ok": True}

    @app.post(f"{API_PREFIX}/agents/enroll", status_code=201)
    def enroll(request: Request, values: Enrollment):
        peer = request.client.host if request.client else None
        if bind is not None and peer == str(bind) and not network.is_loopback:
            # A same-host proxy connects from the coordinator's own VPN address.
            peer = None
        return store.enroll(values.code, values.name, peer=peer)

    @app.post(f"{API_PREFIX}/agents/pairing-code")
    def pairing_code(identity=Depends(agent)):
        return {"code": store.create_code("pair", identity)}

    @app.post(f"{API_PREFIX}/agents/poll")
    def poll(report: AgentReport | None = None, identity=Depends(agent)):
        return store.poll(identity, report)

    @app.get(f"{API_PREFIX}/runs/{{run_id}}/console")
    def console(run_id: UUID, before: int | None = Query(None, ge=0),
                after: int | None = Query(None, ge=0), row=Depends(session)):
        if before is not None and after is not None:
            raise Problem("Elige una sola posición del registro.", 400)
        content, start, end = store.read_console(str(run_id), before, after)
        return Response(content, media_type="application/octet-stream", headers={
            "X-Log-Start": str(start), "X-Log-End": str(end),
        })

    @app.put(f"{API_PREFIX}/runs/{{run_id}}/console")
    async def console_append(request: Request, run_id: UUID, offset: int = Query(ge=0), identity=Depends(agent)):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > CONSOLE_PAGE:
                raise Problem("El bloque de consola supera el tamaño permitido.", 413)
            data.extend(chunk)
        size = await run_in_threadpool(store.append_console, str(run_id), identity, offset, bytes(data))
        return {"offset": size}

    @app.post(f"{API_PREFIX}/runs/{{run_id}}/commands", status_code=202)
    def command(run_id: UUID, values: Command, row=Depends(admin_edit)):
        line = values.command.strip().removeprefix("/").lstrip()
        if not line:
            raise Problem("Escribe un comando de Minecraft.", 400)
        try:
            size = len(line.encode("utf-8"))
        except UnicodeEncodeError:
            raise Problem("El comando contiene texto no válido.", 400) from None
        if size > 4095:
            raise Problem("El comando supera el tamaño permitido.", 400)
        return {"id": store.queue_command(str(run_id), line)}

    @app.get(f"{API_PREFIX}/runs/{{run_id}}/commands/{{command_id}}")
    def command_status(run_id: UUID, command_id: UUID, row=Depends(admin_session)):
        return {"status": store.command_status(str(run_id), str(command_id))}

    @app.post(f"{API_PREFIX}/runs/{{run_id}}/events")
    def event(run_id: UUID, values: Event, identity=Depends(agent)):
        store.event(str(run_id), identity, values.event, values.reason)
        return {"ok": True}

    @app.get(f"{API_PREFIX}/runs/{{run_id}}/snapshot")
    def download(run_id: UUID, identity=Depends(agent)):
        archive = store.snapshot_to_download(str(run_id), identity)
        return FileResponse(archive, media_type="application/gzip", filename=archive.name)

    @app.put(f"{API_PREFIX}/runs/{{run_id}}/snapshot")
    async def upload(request: Request, run_id: UUID, identity=Depends(agent)):
        nonlocal upload_run, upload_activity
        digest = request.headers.get("X-World-SHA256", "")
        length = request.headers.get("Content-Length", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(r"[0-9]{1,12}", length):
            raise Problem("La subida necesita tamaño y SHA-256 válidos.", 400)
        expected = int(length)
        if not 1 <= expected <= MAX_SNAPSHOT_BYTES:
            raise Problem("La copia excede el tamaño permitido.", 413)
        run_id = str(run_id)
        if await run_in_threadpool(store.already_published, run_id, identity, digest):
            return {"snapshot": digest}

        size = 0
        published = False
        checksum = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=store.snapshots, delete=False) as destination:
            path = Path(destination.name)
            activity = {"phase": "uploading", "bytes": 0, "total": expected}
            with upload_lock:
                upload_run, upload_activity = run_id, activity
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > expected:
                        raise Problem("La copia supera el tamaño declarado.", 400)
                    checksum.update(chunk)
                    await run_in_threadpool(destination.write, chunk)
                    with upload_lock:
                        activity["bytes"] = size
                if size != expected or checksum.hexdigest() != digest:
                    raise Problem("Falló la comprobación de integridad de la copia.", 400)
                with upload_lock:
                    activity.update(phase="verifying", bytes=size)
                await run_in_threadpool(destination.flush)
                await run_in_threadpool(os.fsync, destination.fileno())
                await run_in_threadpool(inspect_archive, path)
                await run_in_threadpool(store.publish, run_id, identity, digest, path)
                published = True
            except ClientDisconnect:
                return Response(status_code=400)
            except InvalidSnapshot as error:
                raise Problem(str(error), 400) from error
            finally:
                with upload_lock:
                    if not published:
                        activity.update(phase="retrying", bytes=0)
                path.unlink(missing_ok=True)
        return {"snapshot": digest}

    return app

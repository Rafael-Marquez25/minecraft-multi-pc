import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .protocol import (
    COMMAND_LIFETIME, CONSOLE_PAGE, OFFLINE_AFTER, VERSION, AgentReport, Settings,
    sync_directory, token, token_digest,
)

SESSION_AGE = 12 * 60 * 60
BROWSER_AGE = 365 * 24 * 60 * 60
CODE_AGE = 10 * 60
AUTOMATIC_ENROLLMENT = "enroll-auto"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'user'))
);
CREATE TABLE IF NOT EXISTS sessions (
    digest TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
    csrf TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
    address TEXT, seen REAL
);
CREATE TABLE IF NOT EXISTS browsers (
    digest TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
    agent TEXT NOT NULL REFERENCES agents(id), expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    digest TEXT PRIMARY KEY, kind TEXT NOT NULL,
    agent TEXT REFERENCES agents(id), expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS instance (
    id INTEGER PRIMARY KEY CHECK (id = 1), settings TEXT NOT NULL,
    snapshot TEXT, saved_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, agent TEXT NOT NULL REFERENCES agents(id),
    address TEXT NOT NULL, settings TEXT NOT NULL, base TEXT,
    state TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, reason TEXT, result TEXT,
    created REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(active) WHERE active = 1;
CREATE TABLE IF NOT EXISTS run_activity (
    run TEXT PRIMARY KEY REFERENCES runs(id), progress TEXT
);
CREATE TABLE IF NOT EXISTS console_commands (
    id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), command TEXT NOT NULL,
    created REAL NOT NULL, handed REAL, written INTEGER NOT NULL DEFAULT 0
);
"""


class Problem(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=1,
        maxmem=64 * 1024**2, dklen=32,
    ).hex()


class Store:
    def __init__(self, directory: Path, network: ipaddress.IPv4Network):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = directory / "coordinator.sqlite3"
        self.snapshots = directory / "snapshots"
        self.snapshots.mkdir(exist_ok=True, mode=0o700)
        self.consoles = directory / "consoles"
        self.consoles.mkdir(exist_ok=True, mode=0o700)
        self.network = network
        with closing(sqlite3.connect(self.database, timeout=10, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
        with self.connection(write=True) as db:
            columns = db.execute("PRAGMA table_info(accounts)").fetchall()
            if not any(column["name"] == "role" for column in columns):
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'admin' "
                    "CHECK (role IN ('admin', 'user'))"
                )
            db.execute(
                "INSERT OR IGNORE INTO instance (id, settings) VALUES (1, ?)",
                (Settings().model_dump_json(),),
            )

    @contextmanager
    def connection(self, *, write: bool = False):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_account(self, name: str, password: str) -> None:
        if not 1 <= len(name) <= 64:
            raise Problem("Usa un nombre de 1 a 64 caracteres.", 400)
        if name == "user":
            raise Problem("Crea la cuenta user desde el panel de administración.", 400)
        hashed = self.hash_new_password(password)
        with self.connection(write=True) as db:
            try:
                db.execute("INSERT INTO accounts (name, password) VALUES (?, ?)", (name, hashed))
            except sqlite3.IntegrityError as error:
                raise Problem("Ya existe una cuenta con ese nombre.") from error

    @staticmethod
    def hash_new_password(password: str) -> str:
        if not 12 <= len(password) <= 256:
            raise Problem("Usa una contraseña de 12 a 256 caracteres.", 400)
        salt = secrets.token_hex(16)
        return f"{salt}:{password_hash(password, salt)}"

    def set_user_password(self, password: str) -> None:
        hashed = self.hash_new_password(password)
        with self.connection(write=True) as db:
            account = db.execute(
                "INSERT INTO accounts (name, password, role) VALUES ('user', ?, 'user') "
                "ON CONFLICT(name) DO UPDATE SET password = excluded.password "
                "WHERE accounts.role = 'user' RETURNING id",
                (hashed,),
            ).fetchone()
            if account is None:
                raise Problem("Ya existe una cuenta de administración llamada user.")
            db.execute("DELETE FROM sessions WHERE account = ?", (account["id"],))

    def login(self, name: str, password: str) -> str:
        with self.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
        salt, stored = account["password"].split(":") if account else ("0" * 32, "0" * 64)
        matches = hmac.compare_digest(stored, password_hash(password, salt))
        if not account or not matches:
            raise Problem("Usuario o contraseña incorrectos.", 401)
        session, csrf = token(), token()
        now = time.time()
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            inserted = db.execute(
                "INSERT INTO sessions SELECT ?, id, ?, ? FROM accounts WHERE id = ? AND password = ?",
                (token_digest(session), csrf, now + SESSION_AGE, account["id"], account["password"]),
            )
            if not inserted.rowcount:
                raise Problem("Usuario o contraseña incorrectos.", 401)
        return session

    def session(self, key: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT s.*, a.name, a.role FROM sessions s JOIN accounts a ON a.id = s.account "
                "WHERE s.digest = ? AND s.expires > ?",
                (token_digest(key), time.time()),
            ).fetchone()

    def logout(self, key: str) -> None:
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (token_digest(key),))

    def create_code(self, kind: str, agent: str | None = None) -> str:
        code = secrets.token_urlsafe(18)
        now = time.time()
        with self.connection(write=True) as db:
            db.execute("DELETE FROM codes WHERE expires < ?", (now,))
            db.execute(
                "INSERT INTO codes VALUES (?, ?, ?, ?)",
                (token_digest(code), kind, agent, now + CODE_AGE),
            )
        return code

    @staticmethod
    def consume_code(
        db: sqlite3.Connection, kind: str, code: str, *, alternative_kind: str | None = None,
    ) -> sqlite3.Row:
        row = db.execute(
            "DELETE FROM codes WHERE digest = ? AND kind IN (?, ?) AND expires > ? RETURNING *",
            (token_digest(code), kind, alternative_kind, time.time()),
        ).fetchone()
        if not row:
            raise Problem("El código caducó, ya se usó o es incorrecto.", 400)
        return row

    def enrollment_address(self, peer: str | None) -> str:
        try:
            address = ipaddress.IPv4Address(peer or "")
        except ValueError:
            address = None
        if (
            address is None or address not in self.network or address.is_unspecified or address.is_multicast
            or (self.network.prefixlen < 31 and address in (self.network.network_address, self.network.broadcast_address))
        ):
            raise Problem("Conecta el PC a la VPN y usa la URL HTTPS directa del coordinador para registrarlo.", 400)
        return str(address)

    def enroll(self, code: str, name: str, *, peer: str | None = None) -> dict:
        agent, key = str(uuid.uuid4()), token()
        with self.connection(write=True) as db:
            row = self.consume_code(db, "enroll", code, alternative_kind=AUTOMATIC_ENROLLMENT)
            address = self.enrollment_address(peer) if row["kind"] == AUTOMATIC_ENROLLMENT else None
            db.execute(
                "INSERT INTO agents (id, name, digest, address) VALUES (?, ?, ?, ?)",
                (agent, name, token_digest(key), address),
            )
        return {"id": agent, "token": key}

    def authenticate_agent(self, key: str) -> str:
        with self.connection() as db:
            row = db.execute("SELECT id FROM agents WHERE digest = ?", (token_digest(key),)).fetchone()
        if not row:
            raise Problem("Credencial de agente incorrecta.", 401)
        return row["id"]

    def pair(self, account: int, old_key: str, code: str, *, require_configured: bool) -> str:
        key = token()
        now = time.time()
        with self.connection(write=True) as db:
            row = self.consume_code(db, "pair", code)
            if require_configured:
                host = db.execute("SELECT address FROM agents WHERE id = ?", (row["agent"],)).fetchone()
                if not host["address"] or ipaddress.IPv4Address(host["address"]) not in self.network:
                    raise Problem("Pide al admin que configure la dirección del PC antes de vincularlo.")
            db.execute("DELETE FROM browsers WHERE expires < ?", (now,))
            db.execute("DELETE FROM browsers WHERE account = ? AND digest = ?", (account, token_digest(old_key)))
            db.execute(
                "INSERT INTO browsers VALUES (?, ?, ?, ?)",
                (token_digest(key), account, row["agent"], now + BROWSER_AGE),
            )
        return key

    @staticmethod
    def browser_agent(db: sqlite3.Connection, account: int, key: str) -> str | None:
        row = db.execute(
            "SELECT agent FROM browsers WHERE account = ? AND digest = ? AND expires > ?",
            (account, token_digest(key), time.time()),
        ).fetchone()
        return row["agent"] if row else None

    @staticmethod
    def active(db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM runs WHERE active = 1").fetchone()

    @staticmethod
    def latest(db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()

    @staticmethod
    def online(seen: float | None) -> bool:
        return seen is not None and time.time() - seen < OFFLINE_AFTER

    def overview(self, account: int, browser: str, *, admin: bool) -> dict:
        with self.connection() as db:
            paired = self.browser_agent(db, account, browser)
            instance = db.execute("SELECT * FROM instance").fetchone()
            agents = [
                {"id": row["id"], "name": row["name"], "address": row["address"],
                 "online": self.online(row["seen"])}
                for row in db.execute("SELECT * FROM agents ORDER BY name, id")
            ]
            run = self.active(db)
            latest = self.latest(db)
            activity = db.execute("SELECT progress FROM run_activity WHERE run = ?", (latest["id"],)).fetchone() if latest else None
            visible = run or (latest if latest and latest["state"] == "failed" else None)
            state = "idle"
            current = None
            if visible:
                host = next(agent for agent in agents if agent["id"] == visible["agent"])
                state = visible["state"] if not run or host["online"] else "offline"
                config = Settings.model_validate_json(visible["settings"])
                current = {
                    "id": visible["id"], "host": host["name"], "agent": host["id"],
                    "state": visible["state"], "reason": visible["reason"],
                    "endpoint": f'{visible["address"]}:{config.port}' if state == "running" else None,
                }
            settings = Settings.model_validate_json(instance["settings"])
            overview = {
                "state": state, "run": current, "paired": paired,
                "agents": agents if admin else [host for host in agents if host["id"] == paired],
                "settings": settings.model_dump() if admin else {"version": settings.version, "eula": settings.eula},
                "saved_at": instance["saved_at"], "busy": run is not None,
                "progress": json.loads(activity["progress"]) if state == "starting" and activity and activity["progress"] else None,
                "console": {"run": latest["id"], "commands": admin and state == "running" and activity is not None} if latest else None,
            }
            if admin:
                overview["network"] = str(self.network)
                overview["user_configured"] = db.execute(
                    "SELECT 1 FROM accounts WHERE name = 'user' AND role = 'user'"
                ).fetchone() is not None
            return overview

    def update_settings(self, settings: Settings) -> None:
        if settings.version != VERSION:
            raise Problem("Solo está disponible Minecraft Java 26.3.", 400)
        with self.connection(write=True) as db:
            if self.active(db):
                raise Problem("Detén el servidor y espera a que termine el guardado.")
            instance = db.execute("SELECT * FROM instance").fetchone()
            previous = Settings.model_validate_json(instance["settings"])
            if instance["snapshot"] and settings.seed != previous.seed:
                raise Problem("La semilla está fijada para este mundo.", 400)
            db.execute("UPDATE instance SET settings = ?", (settings.model_dump_json(),))

    def set_address(self, agent: str, address: str) -> None:
        ip = ipaddress.ip_address(address)
        if ip not in self.network or ip.is_unspecified or ip.is_multicast:
            raise Problem(f"Introduce una IPv4 de la red {self.network}.", 400)
        with self.connection(write=True) as db:
            run = self.active(db)
            if run and run["agent"] == agent:
                raise Problem("La dirección de un anfitrión activo queda fijada hasta que termine.")
            updated = db.execute("UPDATE agents SET address = ? WHERE id = ?", (str(ip), agent))
            if not updated.rowcount:
                raise Problem("No se encontró el agente.", 404)

    def start(self, account: int, browser: str) -> tuple[str, bool]:
        with self.connection(write=True) as db:
            run = self.active(db)
            if run:
                return run["id"], False
            agent = self.browser_agent(db, account, browser)
            if not agent:
                raise Problem("Vincula este navegador con el agente de tu PC.")
            host = db.execute("SELECT * FROM agents WHERE id = ?", (agent,)).fetchone()
            if not self.online(host["seen"]):
                raise Problem("El agente vinculado está sin conexión.")
            if not host["address"] or ipaddress.IPv4Address(host["address"]) not in self.network:
                raise Problem(f"Guarda una dirección del agente en la red {self.network} antes de iniciar.")
            instance = db.execute("SELECT * FROM instance").fetchone()
            settings = Settings.model_validate_json(instance["settings"])
            if not settings.eula:
                raise Problem("Acepta la EULA en los ajustes antes de iniciar.")
            previous = self.latest(db)
            run_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO runs (id, agent, address, settings, base, state, created) "
                "VALUES (?, ?, ?, ?, ?, 'starting', ?)",
                (run_id, agent, host["address"], instance["settings"], instance["snapshot"], time.time()),
            )
            db.execute("DELETE FROM run_activity")
            db.execute("DELETE FROM console_commands")
        if previous:
            try:
                (self.consoles / previous["id"]).unlink(missing_ok=True)
            except OSError:
                # The previous log is already inaccessible through the API.
                pass
        return run_id, True

    def stop(self, run_id: str) -> None:
        with self.connection(write=True) as db:
            run = self.require_run(db, run_id)
            self.request_stop(db, run)

    @staticmethod
    def request_stop(db: sqlite3.Connection, run: sqlite3.Row) -> None:
        if run["state"] in {"stopping", "saving"}:
            return
        if run["state"] != "running":
            raise Problem("La ejecución todavía no se puede detener.")
        db.execute("UPDATE runs SET state = 'stopping' WHERE id = ?", (run["id"],))

    def recover(self, run_id: str) -> None:
        with self.connection(write=True) as db:
            run = self.require_run(db, run_id)
            host = db.execute("SELECT seen FROM agents WHERE id = ?", (run["agent"],)).fetchone()
            if self.online(host["seen"]) and run["state"] != "interrupted":
                raise Problem("El anfitrión ha vuelto. Comprueba el estado antes de recuperarlo.")
            db.execute("UPDATE runs SET state = 'revoked', active = 0 WHERE id = ?", (run_id,))

    def poll(self, agent: str, report: AgentReport | None) -> dict:
        with self.connection(write=True) as db:
            now = time.time()
            db.execute("UPDATE agents SET seen = ? WHERE id = ?", (now, agent))
            latest = self.latest(db)
            run = self.active(db)
            result = {"job": None, "latest": latest["id"] if latest else None, "console": None, "command": None}
            if latest and latest["agent"] == agent and report is not None:
                result["console"] = latest["id"]
                if report.written:
                    db.execute(
                        "UPDATE console_commands SET written = 1 WHERE run = ? AND id = ? AND handed IS NOT NULL",
                        (latest["id"], str(report.written)),
                    )
            if run and run["agent"] == agent:
                state = run["state"]
                if report is None:
                    db.execute("DELETE FROM run_activity WHERE run = ?", (run["id"],))
                else:
                    db.execute("INSERT OR IGNORE INTO run_activity (run) VALUES (?)", (run["id"],))
                    if state == "starting" and str(report.run) == run["id"] and report.progress:
                        progress = report.progress.model_dump_json()
                        db.execute("UPDATE run_activity SET progress = ? WHERE run = ? AND progress IS NOT ?", (progress, run["id"], progress))
                    if state == "running":
                        command = self.take_command(db, run["id"], now)
                        if command and command["command"] in {"stop", "minecraft:stop"}:
                            self.request_stop(db, run)
                            state = "stopping"
                            db.execute("UPDATE console_commands SET written = 1 WHERE id = ?", (command["id"],))
                        else:
                            result["command"] = command
                result["job"] = {
                    "id": run["id"], "state": state, "address": run["address"],
                    "settings": Settings.model_validate_json(run["settings"]).model_dump(),
                    "snapshot": run["base"],
                }
            return result

    def read_console(self, run_id: str, before: int | None, after: int | None) -> tuple[bytes, int, int]:
        with self.connection() as db:
            latest = self.latest(db)
            if not latest or latest["id"] != run_id:
                raise Problem("La consola de esa ejecución ya no está disponible.", 410)
        try:
            with (self.consoles / run_id).open("rb") as log:
                size = os.fstat(log.fileno()).st_size
                end = min(size, before) if before is not None else size
                start = max(0, end - CONSOLE_PAGE)
                if after is not None:
                    start = min(after, size)
                    end = min(size, start + CONSOLE_PAGE)
                log.seek(start)
                data = log.read(end - start)
                if after is None and start:
                    # Keep the preceding UTF-8 character on the preceding page.
                    skip = 0
                    while skip < len(data) and (data[skip] & 0xc0) == 0x80:
                        skip += 1
                    start += skip
                    data = data[skip:]
                return data, start, start + len(data)
        except FileNotFoundError:
            return b"", 0, 0

    def append_console(self, run_id: str, agent: str, offset: int, data: bytes) -> int:
        with self.connection(write=True) as db:
            latest = self.latest(db)
            if not latest or latest["id"] != run_id or latest["agent"] != agent:
                raise Problem("La consola de esa ejecución ya no está asignada.", 410)
            with (self.consoles / run_id).open("ab") as log:
                size = log.tell()
                if size == offset and data:
                    log.write(data)
                    size += len(data)
                return size

    def queue_command(self, run_id: str, command: str) -> str:
        with self.connection(write=True) as db:
            run = self.require_run(db, run_id)
            host = db.execute("SELECT seen FROM agents WHERE id = ?", (run["agent"],)).fetchone()
            supported = db.execute("SELECT 1 FROM run_activity WHERE run = ?", (run_id,)).fetchone()
            if run["state"] != "running" or not self.online(host["seen"]) or not supported:
                raise Problem("La consola acepta comandos cuando Minecraft y su agente están conectados.")
            now = time.time()
            db.execute("DELETE FROM console_commands WHERE created < ?", (now - 3600,))
            pending = db.execute(
                "SELECT count(*) FROM console_commands WHERE run = ? AND handed IS NULL AND created >= ?",
                (run_id, now - COMMAND_LIFETIME),
            ).fetchone()[0]
            if pending >= 32:
                raise Problem("La cola de comandos está llena. Espera a que el agente la procese.", 429)
            command_id = str(uuid.uuid4())
            db.execute("INSERT INTO console_commands (id, run, command, created) VALUES (?, ?, ?, ?)", (command_id, run_id, command, now))
            return command_id

    @staticmethod
    def take_command(db: sqlite3.Connection, run_id: str, now: float) -> dict | None:
        if db.execute(
            "SELECT 1 FROM console_commands WHERE run = ? AND handed >= ? AND written = 0",
            (run_id, now - COMMAND_LIFETIME),
        ).fetchone():
            return None
        row = db.execute(
            "SELECT id, command FROM console_commands WHERE run = ? AND handed IS NULL AND created >= ? ORDER BY rowid LIMIT 1",
            (run_id, now - COMMAND_LIFETIME),
        ).fetchone()
        if row:
            db.execute("UPDATE console_commands SET handed = ? WHERE id = ?", (now, row["id"]))
            return dict(row)
        return None

    def command_status(self, run_id: str, command_id: str) -> str:
        with self.connection() as db:
            row = db.execute(
                "SELECT command.created, command.handed, command.written, runs.state FROM console_commands command "
                "JOIN runs ON runs.id = command.run WHERE command.run = ? AND command.id = ?",
                (run_id, command_id),
            ).fetchone()
            if not row:
                raise Problem("El comando ya no está disponible.", 404)
            if row["written"]:
                return "written"
            if time.time() >= (row["handed"] or row["created"]) + COMMAND_LIFETIME or (row["state"] != "running" and not row["handed"]):
                return "unknown"
            return "pending"

    @staticmethod
    def require_run(db: sqlite3.Connection, run_id: str, agent: str | None = None) -> sqlite3.Row:
        run = db.execute("SELECT * FROM runs WHERE id = ? AND active = 1", (run_id,)).fetchone()
        if not run or (agent and run["agent"] != agent):
            raise Problem("La ejecución ya no está asignada.", 410)
        return run

    def event(self, run_id: str, agent: str, event: str, reason: str | None) -> None:
        with self.connection(write=True) as db:
            run = self.require_run(db, run_id, agent)
            state = run["state"]
            if event == "claim" and state == "starting":
                db.execute("UPDATE runs SET claimed = 1 WHERE id = ?", (run_id,))
                return
            if event == "ready" and state in {"running", "stopping", "saving"}:
                return
            if event == "ready" and state == "starting" and run["claimed"]:
                next_state = "running"
            elif event == "saving" and state in {"starting", "running", "stopping", "saving"} and run["claimed"]:
                next_state = "saving"
            elif event == "failed" and state == "starting":
                next_state = "failed"
            elif event == "interrupted" and state in {"starting", "running", "stopping", "saving", "interrupted"}:
                next_state = "interrupted"
            else:
                raise Problem("La ejecución cambió de estado.")
            db.execute(
                "UPDATE runs SET state = ?, active = ?, reason = ? WHERE id = ?",
                (next_state, int(next_state != "failed"), reason, run_id),
            )

    def snapshot_to_download(self, run_id: str, agent: str) -> Path:
        with self.connection() as db:
            run = self.require_run(db, run_id, agent)
            if not run["base"]:
                raise Problem("Esta ejecución empieza con un mundo nuevo.", 404)
            target = self.snapshots / f'{run["base"]}.tar.gz'
            if not target.is_file():
                raise Problem("La copia guardada no está disponible.", 404)
            return target

    @staticmethod
    def check_publication(db: sqlite3.Connection, run_id: str, agent: str, digest: str) -> bool:
        row = db.execute("SELECT * FROM runs WHERE id = ? AND agent = ?", (run_id, agent)).fetchone()
        if row and row["state"] == "done" and row["result"] == digest:
            return True
        if not row or not row["active"]:
            raise Problem("La ejecución ya no está asignada.", 410)
        if row["state"] != "saving":
            raise Problem("La ejecución no está en fase de guardado.")
        return False

    def already_published(self, run_id: str, agent: str, digest: str) -> bool:
        with self.connection() as db:
            return self.check_publication(db, run_id, agent, digest)

    def publish(self, run_id: str, agent: str, digest: str, archive: Path) -> None:
        with self.connection(write=True) as db:
            if self.check_publication(db, run_id, agent, digest):
                return
            target = self.snapshots / f"{digest}.tar.gz"
            archive.replace(target)
            sync_directory(self.snapshots)
            db.execute("UPDATE instance SET snapshot = ?, saved_at = ?", (digest, time.time()))
            db.execute(
                "UPDATE runs SET state = 'done', active = 0, result = ? WHERE id = ?",
                (digest, run_id),
            )

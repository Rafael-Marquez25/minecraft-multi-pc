import json
import logging
import os
import shutil
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .minecraft import Minecraft, MinecraftError
from .protocol import (
    API_PREFIX, COMMAND_LIFETIME, CONSOLE_PAGE, MAX_SNAPSHOT_BYTES, POLL_INTERVAL,
    Job, save_private_json, transfer_size,
)
from .snapshots import InvalidSnapshot, create_archive, digest_file, inspect_archive

log = logging.getLogger(__name__)
START_TIMEOUT = 180


class StaleRun(Exception):
    pass


class Coordinator:
    def __init__(self, url: str, ca: str | None, key: str | None = None):
        address = urlsplit(url)
        if (
            address.scheme != "https" or not address.hostname or address.path.strip("/")
            or address.query or address.fragment or address.username or address.password
        ):
            raise ValueError("Usa la URL HTTPS del coordinador, sin ruta ni credenciales.")
        context = ssl.create_default_context(cafile=ca)
        self.http = httpx.Client(
            base_url=f"{url.rstrip('/')}{API_PREFIX}/",
            verify=context,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=httpx.Timeout(30, connect=5, pool=5),
            trust_env=False,
        )

    def call(self, endpoint: str, payload: dict | None = None) -> dict:
        response = self.http.post(endpoint, json=payload, timeout=8)
        if response.status_code == 410:
            raise StaleRun
        return response.raise_for_status().json()

    def event(self, job: Job, event: str, reason: str | None = None) -> None:
        self.call(f"runs/{job.id}/events", {"event": event, "reason": reason})

    def download(self, job: Job, path: Path, progress=None) -> None:
        if path.exists() and digest_file(path) == job.snapshot:
            return
        temporary = path.with_suffix(".part")
        try:
            if progress:
                progress(0, None)
            with self.http.stream("GET", f"runs/{job.id}/snapshot", headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code == 410:
                    raise StaleRun
                if response.status_code == 404:
                    raise InvalidSnapshot("La copia guardada ya no está disponible en el coordinador.")
                response.raise_for_status()
                total = transfer_size(response.headers)
                if progress:
                    progress(0, total)
                size = 0
                with temporary.open("wb") as file:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_SNAPSHOT_BYTES:
                            raise InvalidSnapshot("La copia descargada supera el límite.")
                        file.write(chunk)
                        if progress:
                            progress(size, total)
            if digest_file(temporary) != job.snapshot:
                raise InvalidSnapshot("La copia descargada no coincide con su SHA-256.")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, job: Job, path: Path) -> None:
        digest = digest_file(path)
        with path.open("rb") as stream:
            response = self.http.put(
                f"runs/{job.id}/snapshot", content=stream,
                headers={"X-World-SHA256": digest, "Content-Length": str(path.stat().st_size)},
                timeout=httpx.Timeout(300, connect=5, pool=5),
            )
        if response.status_code == 410:
            raise StaleRun
        response.raise_for_status()
        if response.json()["snapshot"] != digest:
            raise InvalidSnapshot("El coordinador no confirmó la copia enviada.")

    def send_log(self, run: str, offset: int, data: bytes) -> int:
        response = self.http.put(f"runs/{run}/console", params={"offset": offset}, content=data, timeout=5)
        if response.status_code == 410:
            raise StaleRun
        return response.raise_for_status().json()["offset"]


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"phase": "preparing", "reason": None}

    @property
    def phase(self) -> str:
        return self.data["phase"]

    def save(self, phase: str, reason: str | None = None) -> None:
        state = {"phase": phase, "reason": reason}
        save_private_json(self.path, json.dumps(state))
        self.data = state


class Agent:
    def __init__(self, directory: Path, coordinator: Coordinator, minecraft: Minecraft):
        self.directory = directory
        self.coordinator = coordinator
        self.minecraft = minecraft
        self.shutdown = threading.Event()
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._received = threading.Event()
        self._progress = {}
        self._written = None
        self._command = None
        self._console = None
        self._latest = None
        self.consoles = directory / "consoles"
        self.consoles.mkdir(parents=True, exist_ok=True, mode=0o700)

    def current(self) -> Job | None:
        with self._lock:
            return self._job

    def progress(self, job: Job, phase: str, size: int = 0, total: int | None = None) -> None:
        with self._lock:
            self._progress = {"run": job.id, "progress": {"phase": phase, "bytes": size, "total": total}}

    def heartbeat(self) -> None:
        was_connected = None
        while not self.shutdown.is_set():
            try:
                with self._lock:
                    written = self._written
                    report = {**self._progress, "written": written}
                requested = time.monotonic()
                response = self.coordinator.call("agents/poll", report)
                job = response["job"]
                assignment = Job.model_validate(job) if job else None
                with self._lock:
                    self._job = assignment
                    self._console = response.get("console")
                    if self._written == written:
                        self._written = None
                    if not assignment or assignment.state != "running":
                        self._command = None
                    elif command := response.get("command"):
                        self._command = (assignment.id, requested + COMMAND_LIFETIME, command)
                    latest = response.get("latest")
                    if latest and self._latest != latest:
                        self._latest = latest
                        self.prune_consoles(latest)
                self._received.set()
                if not was_connected:
                    log.info("Conectado al coordinador.")
                was_connected = True
            except (httpx.HTTPError, ValueError) as error:
                if was_connected is not False:
                    log.warning("Coordinador no disponible: %s", error)
                was_connected = False
            self.shutdown.wait(POLL_INTERVAL)

    def prune_consoles(self, latest: str) -> None:
        try:
            for path in self.consoles.glob("*.log"):
                if path.stem != latest:
                    path.unlink(missing_ok=True)
        except OSError as error:
            log.warning("No se pudieron limpiar los logs del agente: %s", error)

    def sync_console(self) -> None:
        previous = None
        offset = 0
        while not self.shutdown.is_set():
            with self._lock:
                run = self._console
            if run != previous:
                previous, offset = run, 0
            try:
                if run:
                    path = self.consoles / f"{run}.log"
                    with path.open("rb") as file:
                        file.seek(offset)
                        data = file.read(CONSOLE_PAGE)
                    if data:
                        offset = self.coordinator.send_log(run, offset, data)
                        continue
            except (FileNotFoundError, StaleRun):
                pass
            except (httpx.HTTPError, OSError, ValueError) as error:
                log.debug("No se pudo enviar el log de Minecraft: %s", error)
            self.shutdown.wait(0.5)

    def serve(self) -> None:
        heartbeat = threading.Thread(target=self.heartbeat, name="heartbeat", daemon=True)
        console = threading.Thread(target=self.sync_console, name="console", daemon=True)
        heartbeat.start()
        console.start()
        try:
            while not self.shutdown.is_set():
                self._received.wait(1)
                job = self.current()
                if job and job.state != "interrupted":
                    try:
                        self.handle(job)
                    except StaleRun:
                        log.info("El coordinador ya no tiene asignada la ejecución %s.", job.id)
                    except (httpx.HTTPError, OSError, InvalidSnapshot, ValueError) as error:
                        log.warning("Ejecución %s: %s", job.id, error)
                self.shutdown.wait(POLL_INTERVAL)
        finally:
            self.shutdown.set()
            heartbeat.join()
            console.join()

    def handle(self, job: Job) -> None:
        run = self.directory / "runs" / job.id
        run.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = Journal(run / "journal.json")
        game = run / "game"
        if journal.phase == "launching" or (journal.phase == "preparing" and job.state != "starting"):
            journal.save("interrupted", "El agente se reinició sin confirmar un cierre normal. El mundo local se conserva.")
        if journal.phase in {"failed", "interrupted"}:
            self.coordinator.event(job, journal.phase, journal.data["reason"])
            return
        if journal.phase == "preparing":
            self.prepare(job, game, run, journal)
        if journal.phase == "stopped" and not self.shutdown.is_set():
            self.publish(job, game, run)

    def prepare(self, job: Job, game: Path, run: Path, journal: Journal) -> None:
        try:
            self.progress(job, "preparing")
            self.minecraft.preflight(job)
            self.minecraft.prepare_jar(lambda size, total: self.progress(job, "jar", size, total))
            if game.exists():
                shutil.rmtree(game)
            game.mkdir(mode=0o700)
            if job.snapshot:
                archive = run / "base.tar.gz"
                self.coordinator.download(job, archive, lambda size, total: self.progress(job, "world", size, total))
                self.progress(job, "extracting")
                inspect_archive(archive, game)
                archive.unlink()
            self.minecraft.write_settings(job, game)
        except (MinecraftError, InvalidSnapshot, OSError) as error:
            journal.save("failed", str(error)[:300])
            self.coordinator.event(job, "failed", journal.data["reason"])
            return
        if self.shutdown.is_set():
            return
        self.progress(job, "launching")
        self.coordinator.event(job, "claim")
        journal.save("launching")
        try:
            process = self.minecraft.launch(job, game)
        except OSError:
            reason = "No se pudo iniciar Java. Consulta el registro local del agente."
            journal.save("failed", reason)
            self.coordinator.event(job, "failed", reason)
            return
        try:
            with self._lock:
                if self._latest == job.id:
                    os.link(game / "server.log", self.consoles / f"{job.id}.log")
        except OSError as error:
            log.warning("No se pudo enlazar el log de Minecraft: %s", error)
        try:
            phase, reason = self.supervise(job, process)
        finally:
            self.minecraft.terminate(process)
        journal.save(phase, reason)
        if phase in {"failed", "interrupted"}:
            self.coordinator.event(job, phase, reason)

    def supervise(self, job: Job, process) -> tuple[str, str | None]:
        deadline = time.monotonic() + START_TIMEOUT
        ready = False
        announced = False
        while process.poll() is None:
            command = self.current()
            if not command or command.id != job.id:
                if ready:
                    self.minecraft.stop(process)
                return "interrupted", "El coordinador retiró la reserva. El mundo local se conserva."
            if self.shutdown.is_set() or command.state == "stopping":
                if ready and self.minecraft.stop(process):
                    return "stopped", None
                return "interrupted", "Minecraft no confirmó un cierre normal. El mundo local se conserva."
            if not ready:
                ready = self.minecraft.ready(job)
                if not ready and time.monotonic() >= deadline:
                    return "failed", "Minecraft no respondió a tiempo. Consulta server.log en el anfitrión."
            if ready and not announced:
                try:
                    self.coordinator.event(job, "ready")
                    announced = True
                except (httpx.TransportError, StaleRun) as error:
                    if isinstance(error, StaleRun):
                        self.minecraft.stop(process)
                        return "interrupted", "El coordinador retiró la reserva. El mundo local se conserva."
                    log.debug("El coordinador aún no confirmó el arranque: %s", error)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code < 500:
                        raise
                    log.debug("El coordinador aún no confirmó el arranque: %s", error)
            if announced and command.state == "running":
                self.write_command(job, process)
            self.shutdown.wait(1)
        if ready:
            return "interrupted", "Java terminó de forma inesperada. El mundo local se conserva."
        return "failed", "Java terminó antes de abrir Minecraft. Consulta server.log en el anfitrión."

    def write_command(self, job: Job, process) -> None:
        with self._lock:
            pending, self._command = self._command, None
        if not pending or pending[0] != job.id or time.monotonic() >= pending[1]:
            return
        command = pending[2]
        try:
            self.minecraft.send_command(process, command["command"])
            with self._lock:
                self._written = command["id"]
        except OSError as error:
            log.debug("No se pudo escribir en la consola de Minecraft: %s", error)

    def publish(self, job: Job, game: Path, run: Path) -> None:
        archive = run / "world.tar.gz"
        self.coordinator.event(job, "saving")
        try:
            if not archive.exists():
                create_archive(game, archive)
            self.coordinator.upload(job, archive)
        except (httpx.HTTPError, OSError, InvalidSnapshot):
            self.coordinator.event(
                job, "saving", "El mundo sigue en el anfitrión. El agente reintentará la subida."
            )
            raise
        shutil.rmtree(run)
        log.info("Copia de la ejecución %s confirmada.", job.id)

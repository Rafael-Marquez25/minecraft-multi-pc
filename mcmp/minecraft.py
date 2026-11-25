import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import httpx

from .protocol import JAVA_VERSION, SERVER_SHA1, SERVER_URL, VERSION, Job, transfer_size
from .snapshots import digest_file

PROTOCOL_VERSION = 777  # Minecraft Java 26.3
JAR_LIMIT = 128 * 1024**2


class MinecraftError(RuntimeError):
    pass


def properties_value(value: str) -> str:
    escaped = []
    encoded = value.encode("utf-16-be")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "big")
        if unit in (32, 92) or not 32 <= unit < 127:
            escaped.append(f"\\u{unit:04x}")
        else:
            escaped.append(chr(unit))
    return "".join(escaped)


def varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7f:
        encoded.append((value & 0x7f) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def read_varint(source) -> int:
    number = 0
    for offset in range(0, 35, 7):
        byte = source.read(1)
        if not byte:
            break
        number |= (byte[0] & 0x7f) << offset
        if not byte[0] & 0x80:
            return number
    raise ValueError("VarInt inválido")


class Minecraft:
    def __init__(self, executable: str, cache: Path):
        self.executable = executable
        self.cache = cache
        self.jar = cache / f"minecraft-{VERSION}.jar"

    def preflight(self, job: Job) -> None:
        java = shutil.which(self.executable)
        if java is None:
            raise MinecraftError(f"Instala Java {JAVA_VERSION} en este anfitrión.")
        try:
            result = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MinecraftError("No se pudo ejecutar Java.") from error
        version = re.search(r'\bversion "(\d+)', result.stderr + result.stdout)
        if result.returncode or not version or int(version[1]) < JAVA_VERSION:
            raise MinecraftError(f"Minecraft {VERSION} requiere Java {JAVA_VERSION} o posterior.")
        self.executable = java

        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((str(job.address), job.settings.port))
        except OSError as error:
            raise MinecraftError(
                f"No se puede abrir {job.address}:{job.settings.port}. "
                "Comprueba la IP VPN y si el puerto está ocupado."
            ) from error

    def prepare_jar(self, progress=None) -> None:
        if self.jar.is_file() and digest_file(self.jar, "sha1") == SERVER_SHA1:
            return
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.jar.with_suffix(".part")
        try:
            size = 0
            if progress:
                progress(0, None)
            with httpx.stream("GET", SERVER_URL, headers={"Accept-Encoding": "identity"}, timeout=30) as response:
                response.raise_for_status()
                total = transfer_size(response.headers)
                if progress:
                    progress(0, total)
                with temporary.open("wb") as file:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > JAR_LIMIT:
                            raise MinecraftError("El JAR supera el tamaño esperado.")
                        file.write(chunk)
                        if progress:
                            progress(size, total)
            if digest_file(temporary, "sha1") != SERVER_SHA1:
                raise MinecraftError("La comprobación del JAR de Mojang falló.")
            temporary.replace(self.jar)
        except httpx.HTTPError as error:
            raise MinecraftError("No se pudo descargar el JAR oficial de Mojang.") from error
        finally:
            temporary.unlink(missing_ok=True)

    def write_settings(self, job: Job, directory: Path) -> None:
        settings = job.settings
        if settings.version != VERSION or not settings.eula:
            raise MinecraftError("Revisa la versión y la aceptación de la EULA en el panel.")
        properties = {
            "server-ip": str(job.address),
            "server-port": str(settings.port),
            "level-name": "world",
            "level-seed": properties_value(settings.seed),
            "difficulty": settings.difficulty,
            "max-players": str(settings.max_players),
            "online-mode": "true",
            "white-list": "false",
            "enforce-whitelist": "false",
            "enable-status": "true",
            "enable-rcon": "false",
            "enable-query": "false",
            "enable-management-server": "false",
        }
        (directory / "server.properties").write_text(
            "".join(f"{name}={value}\n" for name, value in properties.items()), encoding="ascii"
        )
        (directory / "eula.txt").write_text("eula=true\n", encoding="ascii")

    def launch(self, job: Job, directory: Path) -> subprocess.Popen:
        heap = f"-Xmx{job.settings.memory_mib}M"
        with (directory / "server.log").open("ab") as log:
            return subprocess.Popen(
                [self.executable, "-Xms512M", heap, "-jar", str(self.jar), "nogui"],
                cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    @staticmethod
    def ready(job: Job) -> bool:
        address = str(job.address)
        name = address.encode("ascii")
        port = job.settings.port
        handshake = b"\x00" + varint(PROTOCOL_VERSION) + varint(len(name)) + name + port.to_bytes(2) + b"\x01"
        try:
            with socket.create_connection((address, port), timeout=0.5) as connection:
                connection.sendall(varint(len(handshake)) + handshake + b"\x01\x00")
                with connection.makefile("rb") as response:
                    packet = read_varint(response)
                    if not 2 <= packet <= 32_768 or read_varint(response) != 0:
                        return False
                    length = read_varint(response)
                    if not 1 <= length < packet:
                        return False
                    status = json.loads(response.read(length))
                    return status["version"]["protocol"] == PROTOCOL_VERSION
        except (OSError, ValueError, TypeError, KeyError):
            return False

    @staticmethod
    def send_command(process: subprocess.Popen, command: str) -> None:
        descriptor = process.stdin.fileno()
        line = command.encode("utf-8") + b"\n"
        if len(line) > os.fpathconf(descriptor, "PC_PIPE_BUF"):
            raise OSError("El comando supera el tamaño de una escritura en la consola.")
        os.set_blocking(descriptor, False)
        try:
            os.write(descriptor, line)
        finally:
            os.set_blocking(descriptor, True)

    @staticmethod
    def stop(process: subprocess.Popen) -> bool:
        try:
            Minecraft.send_command(process, "stop")
            return process.wait(45) == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdin.close()


import hashlib
import os
import secrets
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, Field

VERSION = "26.3"
JAVA_VERSION = 25
SERVER_SHA1 = "33680f5f2ac32864d6d7cf5e56a705fdb3e05f4c"
SERVER_URL = f"https://piston-data.mojang.com/v1/objects/{SERVER_SHA1}/server.jar"
API_PREFIX = "/api/v1"
POLL_INTERVAL = 2
OFFLINE_AFTER = 15
MAX_SNAPSHOT_BYTES = 10 * 1024**3
MAX_EXPANDED_BYTES = 32 * 1024**3
CHUNK_SIZE = 1024 * 1024
CONSOLE_PAGE = 64 * 1024
COMMAND_LIFETIME = 15


class Settings(BaseModel):
    version: str = VERSION
    memory_mib: int = Field(default=2048, ge=1024, le=65536)
    port: int = Field(default=25565, ge=1024, le=65535)
    seed: str = Field(default="", max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")
    difficulty: str = Field(default="normal", pattern=r"^(peaceful|easy|normal|hard)$")
    max_players: int = Field(default=4, ge=1, le=100)
    eula: bool = False


class Job(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    state: str
    address: IPv4Address
    settings: Settings
    snapshot: str | None = Field(pattern=r"^[a-f0-9]{64}$")


class Progress(BaseModel):
    phase: Literal["preparing", "jar", "world", "extracting", "launching"]
    bytes: int = Field(default=0, ge=0, le=MAX_SNAPSHOT_BYTES)
    total: int | None = Field(default=None, ge=1, le=MAX_SNAPSHOT_BYTES)


class AgentReport(BaseModel):
    run: UUID | None = None
    progress: Progress | None = None
    written: UUID | None = None


def transfer_size(headers: Mapping[str, str]) -> int | None:
    if headers.get("content-encoding", "identity").lower() != "identity":
        return None
    value = headers.get("content-length", "")
    if value.isascii() and value.isdecimal() and len(value) <= 12:
        size = int(value)
        if 0 < size <= MAX_SNAPSHOT_BYTES:
            return size
    return None


def token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_private_json(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(contents)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    sync_directory(path.parent)

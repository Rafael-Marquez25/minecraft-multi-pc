import gzip
import hashlib
import os
import tarfile
import zlib
from pathlib import Path, PurePosixPath

from .protocol import CHUNK_SIZE, MAX_EXPANDED_BYTES, MAX_SNAPSHOT_BYTES, sync_directory


class InvalidSnapshot(ValueError):
    pass


def digest_file(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, algorithm).hexdigest()


class ExpandedArchive:
    def __init__(self, source):
        self.source = source
        # Allow for tar headers and padding as well as file contents.
        self.remaining = MAX_EXPANDED_BYTES + 256 * 1024**2

    def read(self, size: int) -> bytes:
        chunk = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise InvalidSnapshot("La copia supera el límite de extracción.")
        return chunk


def inspect_archive(path: Path, destination: Path | None = None) -> None:
    """Check the entire world archive, optionally extracting to an empty directory."""
    seen: set[PurePosixPath] = set()
    files: set[PurePosixPath] = set()
    parents: set[PurePosixPath] = set()
    total_size = 0
    level = PurePosixPath("world/level.dat")
    try:
        with gzip.open(path, "rb") as compressed:
            expanded = ExpandedArchive(compressed)
            with tarfile.open(fileobj=expanded, mode="r|") as archive:
                for member in archive:
                    name = PurePosixPath(member.name)
                    if (
                        not name.parts or name.parts[0] != "world" or ".." in name.parts
                        or "\0" in member.name or name in seen or files.intersection(name.parents)
                        or not (member.isfile() or member.isdir()) or member.issparse()
                        or name == PurePosixPath("world/session.lock")
                        or (member.isfile() and (len(name.parts) == 1 or name in parents))
                        or member.size < 0 or (member.isdir() and member.size != 0)
                    ):
                        raise InvalidSnapshot("La copia contiene rutas o tipos de archivo no permitidos.")
                    seen.add(name)
                    parents.update(name.parents)
                    total_size += member.size
                    if total_size > MAX_EXPANDED_BYTES or len(seen) > 250_000:
                        raise InvalidSnapshot("La copia supera el límite de extracción.")
                    if member.isfile():
                        files.add(name)
                        with archive.extractfile(member) as source:
                            if destination is None:
                                while source.read(CHUNK_SIZE):
                                    pass
                            else:
                                target = destination.joinpath(*name.parts)
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with target.open("xb") as output:
                                    while chunk := source.read(CHUNK_SIZE):
                                        output.write(chunk)
                    elif destination is not None:
                        destination.joinpath(*name.parts).mkdir(parents=True, exist_ok=True)
            # Verify the gzip footer and bound the data after the last tar block.
            if len(expanded.read(CHUNK_SIZE + 1)) > CHUNK_SIZE:
                raise InvalidSnapshot("La copia contiene datos adicionales.")
        if level not in files:
            raise InvalidSnapshot("Falta world/level.dat en la copia.")
    except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
        raise InvalidSnapshot("La copia está dañada o no se pudo leer.") from error


def create_archive(directory: Path, target: Path) -> None:
    def include(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if info.name == "world/session.lock" else info

    temporary = target.with_suffix(".part")
    try:
        with temporary.open("wb") as file:
            with tarfile.open(fileobj=file, mode="w:gz", compresslevel=3) as archive:
                archive.add(directory / "world", arcname="world", filter=include)
            file.flush()
            os.fsync(file.fileno())
        if temporary.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise InvalidSnapshot("La copia supera el límite de subida.")
        temporary.replace(target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)

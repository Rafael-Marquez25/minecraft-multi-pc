import argparse
import getpass
import json
import logging
import os
import signal
import socket
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import httpx

from .protocol import save_private_json

DEFAULT_ROOT = Path(
    os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
) / "minecraft-multi-pc"


def coordinator_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_ROOT / "coordinator")


def agent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_ROOT / "agent")


def parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcmp", description="Minecraft Multi-PC")
    programs = parser.add_subparsers(dest="program", required=True)
    coordinator = programs.add_parser("coordinator", help="Panel y almacenamiento central")
    actions = coordinator.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Crear una cuenta del panel")
    coordinator_options(init)
    init.add_argument("--user", default="admin")
    serve = actions.add_parser("serve", help="Servir el panel por HTTPS")
    coordinator_options(serve)
    serve.add_argument("--bind", type=IPv4Address, default=IPv4Address("127.0.0.1"))
    serve.add_argument("--network", type=IPv4Network, default=IPv4Network("127.0.0.0/8"))
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--cert", type=Path, required=True)
    serve.add_argument("--key", type=Path, required=True)

    agent = programs.add_parser("agent", help="Anfitrión Minecraft local")
    actions = agent.add_subparsers(dest="action", required=True)
    enroll = actions.add_parser("enroll", help="Registrar este PC con un código del panel")
    agent_options(enroll)
    enroll.add_argument("--url", required=True)
    enroll.add_argument("--ca", type=Path, help="Certificado de la CA de pruebas")
    enroll.add_argument("--name", default=socket.gethostname()[:64])
    pair = actions.add_parser("pair", help="Mostrar un código para vincular este navegador")
    agent_options(pair)
    run = actions.add_parser("run", help="Ejecutar el agente en primer plano")
    agent_options(run)
    run.add_argument("--java", default="java", help="Ejecutable de Java 25")
    return parser.parse_args()


def run_coordinator(args: argparse.Namespace) -> None:
    import uvicorn

    from .store import Store
    from .web import create_app

    if args.action == "init":
        password = getpass.getpass("Contraseña (mínimo 12 caracteres): ")
        if password != getpass.getpass("Repite la contraseña: "):
            raise ValueError("Las contraseñas no coinciden.")
        Store(args.data_dir, IPv4Network("127.0.0.0/8")).add_account(args.user, password)
        print(f"Cuenta {args.user} creada.")
        return
    if args.bind not in args.network or args.bind.is_unspecified or args.bind.is_multicast:
        raise ValueError("--bind debe ser una IPv4 de --network.")
    if not 1024 <= args.port <= 65535:
        raise ValueError("--port debe estar entre 1024 y 65535.")
    uvicorn.run(
        create_app(args.data_dir, args.network), host=str(args.bind), port=args.port,
        ssl_certfile=str(args.cert), ssl_keyfile=str(args.key), proxy_headers=False,
    )


def run_agent(args: argparse.Namespace) -> None:
    import fcntl

    from .agent import Agent, Coordinator
    from .minecraft import Minecraft

    directory = args.state_dir.expanduser().resolve()
    config = directory / "agent.json"
    if args.action == "enroll":
        if config.exists():
            raise ValueError("Este directorio de estado ya tiene un agente registrado.")
        code = getpass.getpass("Código de registro del panel: ").strip()
        ca = str(args.ca.expanduser().resolve()) if args.ca else None
        coordinator = Coordinator(args.url, ca)
        with coordinator.http:
            identity = coordinator.call("agents/enroll", {"name": args.name, "code": code})
        save_private_json(config, json.dumps({"url": args.url, "ca": ca, "key": identity["token"]}))
        print("Agente registrado. Genera un código con mcmp agent pair.")
        return
    if not config.exists():
        raise ValueError("Primero registra este PC con mcmp agent enroll.")
    settings = json.loads(config.read_text())
    coordinator = Coordinator(settings["url"], settings["ca"], settings["key"])
    with coordinator.http:
        if args.action == "pair":
            print(coordinator.call("agents/pairing-code")["code"])
            return
        with (directory / "agent.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Ya hay un agente usando este directorio de estado.") from error
            agent = Agent(directory, coordinator, Minecraft(args.java, directory / "cache"))
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: agent.shutdown.set())
            agent.serve()


def main() -> None:
    from .store import Problem

    args = parse_cli()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.program == "coordinator":
            run_coordinator(args)
        else:
            run_agent(args)
    except httpx.HTTPStatusError as error:
        try:
            detail = error.response.json()["detail"]
        except (ValueError, KeyError):
            detail = str(error)
        raise SystemExit(f"Error: {detail}") from None
    except (ValueError, OSError, Problem, httpx.TransportError) as error:
        raise SystemExit(f"Error: {error}") from None


if __name__ == "__main__":
    main()

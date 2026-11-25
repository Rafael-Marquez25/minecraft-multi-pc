#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo/scripts/lib/deploy.sh"

base=$repo/.local/local
data=$base/coordinator
state=$base/agent
tls=$base/tls
python=$base/venv/bin/python
coordinator_log=$base/coordinator.log
bind=127.0.0.1
network=127.0.0.1/32
java=java
requested_port=

usage() {
    printf 'Uso: %s [--port PUERTO] [--java EJECUTABLE]\n       %s pair\n' "$0" "$0"
}

pairing_step() {
    if [[ -t 2 && ${TERM:-dumb} != dumb && -z ${NO_COLOR:-} ]]; then
        printf '\n\033[1;33m%s\033[0m\n' "$1" >&2
    else
        printf '\n%s\n' "$1" >&2
    fi
    shift
    printf '  %s\n' "$@" >&2
}

show_pairing_code() {
    pairing_step "$1" 'En la web, abre «Vincular navegador» o «Cambiar vinculación» y pega el código que aparece abajo.'
    mcmp agent pair --state-dir "$state"
}

if [[ ${1:-} == pair && $# -eq 1 ]]; then
    [[ -x $python && -f $state/agent.json ]] || die 'Primero inicia la prueba y registra el agente.'
    show_pairing_code 'Vincular el navegador'
    exit
fi

while (($#)); do
    case $1 in
        --port) requested_port=$(parse_port "${2:-}"); shift 2 ;;
        --java) java=${2:?Indica el ejecutable de Java.}; shift 2 ;;
        -h|--help) usage; exit ;;
        *) usage >&2; die "Opción desconocida: $1." ;;
    esac
done

require_tools python3 openssl curl flock setsid "$java"
java_version=$("$java" -version 2>&1) || die "No se pudo ejecutar $java."
[[ $java_version =~ version\ \"([0-9]{1,3}) && ${BASH_REMATCH[1]} -ge 25 ]] ||
    die 'Minecraft requiere Java 25 o posterior. Usa --java para seleccionar el ejecutable.'

lock_deployment
port=${requested_port:-8443}
if [[ -f $base/port ]]; then
    port=$(parse_port "$(<"$base/port")")
    [[ -z $requested_port || $requested_port == "$port" ]] ||
        die "Esta prueba ya usa el puerto $port. Iníciala sin --port para reutilizarlo."
fi
url=https://$bind:$port
check_listener
install_project
create_tls Local
ensure_account
printf '%s\n' "$port" > "$base/port.tmp"
mv -T -- "$base/port.tmp" "$base/port"

coordinator_pid=
agent_pid=
cleanup() {
    if [[ -n $agent_pid ]]; then
        kill -TERM "$agent_pid" 2>/dev/null || true
        wait "$agent_pid" 2>/dev/null || true
    fi
    if [[ -n $coordinator_pid ]]; then
        kill -TERM "$coordinator_pid" 2>/dev/null || true
        wait "$coordinator_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

setsid -- "$python" -I -m mcmp.cli coordinator serve --data-dir "$data" \
    --bind "$bind" --network "$network" --port "$port" \
    --cert "$tls/panel.pem" --key "$tls/panel-key.pem" >>"$coordinator_log" 2>&1 9>&- &
coordinator_pid=$!
wait_for_panel "$coordinator_pid" || die "El panel no respondió por HTTPS en $url. Consulta $coordinator_log."
printf '\nPanel: %s/\nUsuario: admin\nCA para importar en el navegador: %s/ca.pem\nLogs del panel: %s\n' \
    "$url" "$tls" "$coordinator_log"

if [[ ! -f $state/agent.json ]]; then
    [[ -t 0 ]] || die 'Abre una terminal interactiva para registrar el agente.'
    pairing_step '1. Registrar el PC' \
        'En la web, abre «Registrar un PC Linux» y genera un código.' \
        'Pégalo en esta terminal y pulsa Intro. El código no aparece al pegarlo.'
    mcmp agent enroll --state-dir "$state" --url "$url" --ca "$tls/ca.pem"
    show_pairing_code '2. Vincular el navegador'
fi

"$python" -I - "$data" "$state/agent.json" "$network" "$bind" <<'PY'
import json
import sys
from ipaddress import IPv4Network
from pathlib import Path

from mcmp.store import Store

directory, identity, network, address = sys.argv[1:]
store = Store(Path(directory), IPv4Network(network))
agent = store.authenticate_agent(json.loads(Path(identity).read_text())["key"])
with store.connection() as db:
    configured = db.execute("SELECT address FROM agents WHERE id = ?", (agent,)).fetchone()["address"]
if configured != address:
    store.set_address(agent, address)
PY

printf '\nAcepta la EULA en «Ajustes de la partida». Antes de cerrar con Ctrl+C, usa «Detener y guardar».\n'
setsid -- "$python" -I -m mcmp.cli agent run --state-dir "$state" --java "$java" 9>&- &
agent_pid=$!
wait -n "$coordinator_pid" "$agent_pid" || true
die "El coordinador o el agente ha terminado. Revisa esta terminal y $coordinator_log."

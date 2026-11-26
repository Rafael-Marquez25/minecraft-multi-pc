# Funciones compartidas por los scripts de despliegue.

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_tools() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null || die "Falta $tool. Consulta los requisitos en el README."
    done
    python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die 'Instala Python 3.12 o posterior.'
}

require_java() {
    local java=$1 version
    version=$("$java" -version 2>&1) || die "No se pudo ejecutar $java."
    [[ $version =~ version\ \"([0-9]{1,3}) && ${BASH_REMATCH[1]} -ge 25 ]] ||
        die 'Minecraft requiere Java 25 o posterior. Usa --java para seleccionar el ejecutable.'
}

lock_deployment() {
    mkdir -p -- "$base"
    exec 9>"$base/deploy.lock"
    flock -n 9 || die 'Ya hay un despliegue en marcha en este directorio.'
}

parse_port() {
    [[ $1 =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 )) ||
        die 'El puerto debe estar entre 1024 y 65535.'
    printf '%d\n' "$((10#$1))"
}

unit_argument() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "$value"
}

check_listener() {
    python3 -I - "$bind" "$port" <<'PY'
import errno
import socket
import sys

ip, port = sys.argv[1:]
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((ip, int(port)))
    except OSError as error:
        detail = {
            errno.EADDRINUSE: f"El puerto {port} ya está ocupado en {ip}. Comprueba qué proceso lo usa.",
            errno.EADDRNOTAVAIL: f"La IP {ip} no está asignada a este equipo. Comprueba la VPN.",
        }.get(error.errno, f"No se puede escuchar en {ip}:{port}. {error.strerror}.")
        sys.exit(f"Error: {detail}")
PY
}

install_project() {
    if [[ -f $base/installed ]]; then
        [[ -x $python ]] || die "Falta el entorno instalado en $base/venv. Consulta el README."
        return
    fi
    python3 -I -m venv "$base/venv" || die 'No se pudo crear el entorno. En Ubuntu instala python3-venv.'
    "$python" -I -m pip --disable-pip-version-check install "$repo"
    touch -- "$base/installed"
}

create_tls() (
    if [[ ! -e $tls ]]; then
        local staging
        staging=$(mktemp -d "$base/.tls-XXXXXX")
        trap 'rm -rf -- "$staging"' EXIT
        cd -- "$staging"
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ca-key.pem
        openssl req -new -x509 -sha256 -days 3650 -key ca-key.pem -out ca.pem \
            -subj "/CN=Minecraft Multi-PC $1 ($bind)" -config /dev/null \
            -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
            -addext 'keyUsage=critical,keyCertSign,cRLSign'
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out panel-key.pem
        openssl req -new -key panel-key.pem -out panel.csr -subj "/CN=$bind" -config /dev/null
        cat > panel.ext <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=IP:$bind
EOF
        openssl x509 -req -in panel.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
            -sha256 -days 365 -extfile panel.ext -out panel.pem
        rm -- panel.csr panel.ext ca.srl
        mv -T -- "$staging" "$tls"
    fi
    [[ -s $tls/panel-key.pem ]] &&
        openssl verify -purpose sslserver -verify_ip "$bind" -CAfile "$tls/ca.pem" "$tls/panel.pem" >/dev/null ||
        die "Los certificados en $tls no son válidos para $bind."
)

mcmp() {
    "$python" -I -m mcmp.cli "$@"
}

ensure_account() {
    local present=no
    if [[ -f $data/coordinator.sqlite3 ]]; then
        present=$("$python" -I - "$data" "$network" <<'PY'
import sys
from ipaddress import IPv4Network
from pathlib import Path

from mcmp.store import Store

store = Store(Path(sys.argv[1]), IPv4Network(sys.argv[2]))
with store.connection() as db:
    account = db.execute("SELECT 1 FROM accounts WHERE name = ?", ("admin",)).fetchone()
print("yes" if account else "no")
PY
        )
    fi
    if [[ $present == no ]]; then
        [[ -t 0 ]] || die 'Abre una terminal interactiva para crear la cuenta admin.'
        mcmp coordinator init --data-dir "$data" --user admin
    fi
}

wait_for_panel() {
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        if curl --noproxy '*' --max-time 1 --silent --fail --cacert "$tls/ca.pem" "$url/" >/dev/null; then
            return
        fi
        if [[ $# -gt 0 ]] && ! kill -0 "$1" 2>/dev/null; then
            return 1
        fi
        sleep 0.25
    done
    return 1
}

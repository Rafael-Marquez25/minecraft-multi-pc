#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo/scripts/lib/deploy.sh"

base=$repo/.local/server
data=$base/data
tls=$base/tls
python=$base/venv/bin/python
service=mcmp-coordinator.service
unit_file=/etc/systemd/system/$service
bind=
network=
port=8443
nginx_hostname=

usage() {
    printf 'Uso: %s --bind IP-VPN --network CIDR-VPN [--port PUERTO] [--nginx-hostname DOMINIO]\n' "$0"
}

while (($#)); do
    case $1 in
        --bind) bind=${2:?Indica la IPv4 de la VPN.}; shift 2 ;;
        --network) network=${2:?Indica la subred de la VPN.}; shift 2 ;;
        --port) port=$(parse_port "${2:-}"); shift 2 ;;
        --nginx-hostname) nginx_hostname=${2:?Indica el nombre DNS del sitio de Nginx.}; shift 2 ;;
        -h|--help) usage; exit ;;
        *) usage >&2; die "Opción desconocida: $1." ;;
    esac
done

[[ -n $bind && -n $network ]] || { usage >&2; die 'Indica --bind y --network.'; }
(( EUID != 0 )) || die 'Ejecuta el script como tu usuario habitual. El script pedirá sudo para el servicio.'
require_tools python3 openssl curl flock systemctl sudo
user=$(id -un)
[[ $repo != *[[:cntrl:]]* ]] || die 'La ruta del clon contiene caracteres de control.'
nginx_hostname=${nginx_hostname,,}
python3 -I - "$bind" "$network" "$nginx_hostname" <<'PY'
import re
import sys
from ipaddress import IPv4Address, IPv4Network

address, subnet, hostname = sys.argv[1:]
try:
    ip = IPv4Address(address)
    network = IPv4Network(subnet)
except ValueError:
    sys.exit("Error: Indica una IPv4 y una subred IPv4 en formato CIDR, como 10.8.0.1 y 10.8.0.0/24.")
if "/" not in subnet or not subnet.split("/", 1)[1].isdigit():
    sys.exit("Error: Usa una longitud de prefijo CIDR, por ejemplo /24.")
if ip not in network or ip.is_unspecified or ip.is_multicast:
    sys.exit("Error: --bind debe ser una IPv4 utilizable de --network.")
label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if hostname and (
    len(hostname) > 251
    or not re.fullmatch(rf"(?:{label}\.)+{label}", hostname)
    or hostname.rsplit(".", 1)[-1].isdigit()
):
    sys.exit("Error: --nginx-hostname debe ser un nombre DNS completo de hasta 251 caracteres, como mc.home.arpa.")
PY
url=https://$bind:$port
systemctl show --property=Version --value >/dev/null || die 'El sistema debe estar arrancado con systemd.'

lock_deployment
candidate=$(mktemp "$base/.unit-XXXXXX")
staged_unit=
cleanup() {
    rm -f -- "$candidate"
    if [[ -n $staged_unit ]]; then
        sudo rm -f -- "$staged_unit"
    fi
}
trap cleanup EXIT

install_nginx_tls() (
    local hostname=$1
    local key=$tls/$hostname.key certificate=$tls/$hostname.pem
    local destination=/etc/nginx/minecraft-tls
    local staging signing_key public_key
    staging=$(mktemp -d "$tls/.nginx-XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT

    signing_key=$key
    if [[ ! -f $key ]] || ! public_key=$(openssl pkey -in "$key" -passin pass: -pubout 2>/dev/null); then
        signing_key=$staging/site.key
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$signing_key"
    fi

    if [[ $signing_key != "$key" ]] ||
        ! openssl x509 -in "$certificate" -noout -checkend "$((30 * 24 * 60 * 60))" >/dev/null 2>&1 ||
        ! openssl verify -purpose sslserver -verify_hostname "$hostname" \
            -CAfile "$tls/ca.pem" "$certificate" >/dev/null 2>&1 ||
        [[ $public_key != "$(openssl x509 -in "$certificate" -noout -pubkey)" ]]; then
        openssl req -new -x509 -sha256 -days 365 -key "$signing_key" -passin pass: \
            -CA "$tls/ca.pem" -CAkey "$tls/ca-key.pem" -config /dev/null \
            -subj '/CN=Minecraft Multi-PC' -out "$staging/site.pem" \
            -addext "subjectAltName=DNS:$hostname" \
            -addext 'basicConstraints=critical,CA:FALSE' \
            -addext 'keyUsage=critical,digitalSignature' \
            -addext 'extendedKeyUsage=serverAuth'
        openssl verify -purpose sslserver -verify_hostname "$hostname" \
            -CAfile "$tls/ca.pem" "$staging/site.pem" >/dev/null
        if [[ $signing_key != "$key" ]]; then
            mv -T -- "$signing_key" "$key"
        fi
        mv -T -- "$staging/site.pem" "$certificate"
    fi

    sudo install -d -m 0755 -- "$destination"
    sudo install -C -m 0600 -- "$key" "$destination/"
    sudo install -C -m 0644 -- "$certificate" "$tls/ca.pem" "$destination/"
    printf 'Certificados para Nginx: %s/%s.pem, %s.key y ca.pem\n' "$destination" "$hostname" "$hostname"
    printf 'Si el sitio ya está configurado, aplica el certificado con: sudo nginx -t && sudo systemctl reload nginx\n'
)

cat > "$candidate" <<EOF
[Unit]
Description=Coordinador Minecraft Multi-PC
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=exec
User=$user
UMask=0077
ExecStart=:$(unit_argument "$python") -I -m mcmp.cli coordinator serve --data-dir $(unit_argument "$data") --bind $bind --network $network --port $port --cert $(unit_argument "$tls/panel.pem") --key $(unit_argument "$tls/panel-key.pem")
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

fragment=$(systemctl show "$service" --property=FragmentPath --value)
load_state=$(systemctl show "$service" --property=LoadState --value)
existing=no
if [[ $load_state != not-found || -e $unit_file || -L $unit_file ]]; then
    [[ -z $fragment || $fragment == "$unit_file" ]] && [[ ! -L $unit_file ]] &&
        cmp -s -- "$candidate" "$unit_file" ||
        die "$service pertenece a otra instalación o usa otros parámetros. Revisa 'systemctl cat $service'."
    existing=yes
fi
if [[ $existing == no ]] || ! systemctl is-active --quiet "$service"; then
    check_listener
fi

sudo -v
install_project
create_tls VPN
ensure_account

if [[ $existing == no ]]; then
    staged_unit=$(sudo mktemp "$unit_file.XXXXXX")
    sudo install -m 0644 -- "$candidate" "$staged_unit"
    sudo ln -T -- "$staged_unit" "$unit_file" || die "Ya existe $unit_file. Revisa el servicio antes de continuar."
fi
sudo systemctl daemon-reload
sudo systemctl enable --now "$service"
wait_for_panel || die "El panel no respondió por HTTPS en $url. Consulta 'journalctl -u $service'."

printf '\nPanel: %s/\nUsuario: admin\nCA pública para copiar a los PCs: %s/ca.pem\n' "$url" "$tls"
printf 'Logs del servicio: journalctl -u %s -f\n' "$service"
if [[ -n $nginx_hostname ]]; then
    install_nginx_tls "$nginx_hostname"
fi

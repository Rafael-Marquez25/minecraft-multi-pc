#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo/scripts/lib/deploy.sh"

base=$repo/.local/server
data=$base/data
tls=$base/tls
python=$base/venv/bin/python
service=mcmp-coordinator.service
update=no
reload_nginx=no

usage() {
    printf 'Uso: %s [--update] [--reload-nginx]\n' "$0"
    printf '  --update        Instala el código del clon antes de reiniciar.\n'
    printf '  --reload-nginx  Valida y recarga Nginx después del reinicio.\n'
}

while (($#)); do
    case $1 in
        --update) update=yes ;;
        --reload-nginx) reload_nginx=yes ;;
        -h|--help) usage; exit ;;
        *) usage >&2; die "Opción desconocida: $1." ;;
    esac
    shift
done

(( EUID != 0 )) || die 'Ejecuta el script como el usuario que instaló el coordinador.'
require_tools curl flock systemctl sudo
[[ -x $python && -s $tls/ca.pem ]] || die 'Ejecuta el script desde el clon donde está desplegado el coordinador.'
lock_deployment

configuration=$(systemctl cat --no-pager "$service") ||
    die "No se pudo leer $service. Consulta 'systemctl status $service'."
exec_start=$(sed -n 's/^[[:space:]]*ExecStart[[:space:]]*=[[:space:]]*//p' <<< "$configuration" | tail -n 1)
prefix=":$(unit_argument "$python") -I -m mcmp.cli coordinator serve --data-dir $(unit_argument "$data") "
[[ $exec_start == "$prefix"* ]] ||
    die "$service usa otro clon o una unidad personalizada. Revisa 'systemctl cat $service'."

arguments=${exec_start#"$prefix"}
endpoint_pattern='^--bind ([0-9.]+) --network [0-9./]+ --port ([0-9]+) --cert '
[[ $arguments =~ $endpoint_pattern ]] ||
    die "No se pudo leer la IP y el puerto de $service."
url=https://${BASH_REMATCH[1]}:${BASH_REMATCH[2]}

sudo -v
if [[ $reload_nginx == yes ]]; then
    sudo nginx -t || die 'Corrige la configuración de Nginx antes de reintentar.'
fi

if [[ $update == yes ]]; then
    "$python" -I -m pip --disable-pip-version-check install "$repo" ||
        die 'La instalación del paquete falló.'
fi

sudo systemctl restart "$service"
wait_for_panel || die "El panel no respondió por HTTPS en $url. Consulta 'journalctl -u $service -n 50'."

if [[ $reload_nginx == yes ]]; then
    sudo systemctl reload nginx
fi
printf 'Coordinador disponible en %s/\n' "$url"

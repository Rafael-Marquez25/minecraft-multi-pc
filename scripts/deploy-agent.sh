#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo/scripts/lib/deploy.sh"

base=$repo/.local/vpn-agent
python=$base/venv/bin/python
config=$base/agent.json
default_ca=$repo/.local/vpn-ca.pem
url=
ca=
java=java

usage() {
    printf 'Primer registro: %s --url URL_HTTPS [--ca ARCHIVO] [--java EJECUTABLE]\n' "$0"
    printf 'Arrancar:        %s [--java EJECUTABLE]\n' "$0"
    printf 'Vincular:        %s pair\n' "$0"
    printf 'CA pública predeterminada: %s\n' "$default_ca"
}

if [[ ${1:-} == pair && $# -eq 1 ]]; then
    [[ -x $python && -f $config ]] || die 'Primero ejecuta el script para instalar y registrar el agente.'
    printf 'Pega el código en «Vincular navegador» en el panel.\n' >&2
    mcmp agent pair --state-dir "$base"
    exit
fi

while (($#)); do
    case $1 in
        --url|--ca|--java)
            [[ -n ${2:-} && $2 != --* ]] || die "Falta el valor de $1."
            case $1 in
                --url) url=$2 ;;
                --ca) ca=$2 ;;
                --java) java=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit ;;
        *) usage >&2; die "Opción desconocida: $1." ;;
    esac
done

require_tools python3 flock "$java"
require_java "$java"
lock_deployment

if [[ -f $config ]]; then
    [[ -z $url && -z $ca ]] || die 'Este PC ya está registrado. Arranca el agente sin --url ni --ca.'
    install_project
else
    [[ $url == https://?* ]] || die 'Indica la URL HTTPS del coordinador con --url.'
    ca=${ca:-$default_ca}
    [[ -f $ca && -r $ca && -s $ca ]] || die "Guarda la CA pública del coordinador en $ca o indica su ruta con --ca."
    [[ -t 0 ]] || die 'Abre una terminal interactiva para registrar el agente.'
    install_project

    printf '\nEn el panel, genera un código en «Registrar un PC Linux» y pégalo cuando se solicite.\n' >&2
    mcmp agent enroll --state-dir "$base" --url "$url" --ca "$ca"
    printf '\nDespués, pega este código en «Vincular navegador» en el panel.\n' >&2
    if ! mcmp agent pair --state-dir "$base"; then
        printf 'Genera otro código con «%s pair» desde otra terminal.\n' "$0" >&2
    fi
fi

printf '\nAgente en primer plano. Antes de cerrar con Ctrl+C, usa «Detener y guardar» en el panel.\n' >&2
exec "$python" -I -m mcmp.cli agent run --state-dir "$base" --java "$java"

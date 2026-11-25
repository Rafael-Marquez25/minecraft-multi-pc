# Minecraft Multi-PC

Un panel web para iniciar una partida de Minecraft Java Vanilla 26.3 en un PC
Linux, guardar el mundo en un coordinador y continuar la partida en otro PC.
Cuando Minecraft ya está en marcha, el panel indica qué equipo lo aloja y
permite copiar su dirección.

En **Partida** puedes seguir las descargas, el arranque y la subida del
guardado. **Consola de Minecraft** muestra el registro de la última ejecución,
incluido el cierre. Mientras Minecraft esté en marcha puedes enviar comandos
como `list` o `say Hola`. `stop` usa el mismo guardado que **Detener y guardar**.

## Requisitos de despliegue

Usa Linux con Bash, Python 3.12 o posterior, el módulo `venv`, OpenSSL, curl y
`util-linux`. Los scripts crean su propio entorno e instalan el proyecto desde
la raíz del clon. En Ubuntu puedes instalar los requisitos con estos comandos.

```sh
sudo apt update
sudo apt install python3 python3-venv openssl curl util-linux
```

Los PCs que alojan la partida necesitan Java 25 o posterior. En Ubuntu 26.04
puedes instalarlo con `sudo apt install openjdk-25-jre-headless`. El agente
descarga el JAR oficial de Mojang y verifica su checksum al iniciar la primera
partida. Para entrar en la partida, usa Minecraft Java 26.3.

## Prueba en un PC Linux

1. Desde el clon, ejecuta `./scripts/deploy-local.sh`. En la primera ejecución,
   crea la contraseña de `admin` en la terminal. Si prefieres otro puerto para
   el panel, elígelo al instalar, por ejemplo con `--port 8444`. Puedes elegir
   otro ejecutable de Java con `--java /ruta/a/bin/java`.
2. Importa `.local/local/tls/ca.pem` como autoridad de confianza en el navegador
   y abre <https://127.0.0.1:8443> o la URL que indique el script. Inicia sesión,
   genera un código en **Registrar un PC Linux** e introdúcelo en la terminal.
   El código se introduce de forma oculta. Pulsa Intro y el script te dará el
   código para **Vincular navegador**. Para pedir otro,
   ejecuta `./scripts/deploy-local.sh pair` desde una segunda terminal.
3. En **Ajustes de la partida**, acepta la EULA de Minecraft y guarda. Pulsa
   **Iniciar** y conéctate a la dirección que muestra el panel. Para cerrar la
   prueba, pulsa **Detener y guardar**, espera la confirmación de la copia y
   luego pulsa `Ctrl+C` en la terminal.

Vuelve a ejecutar `./scripts/deploy-local.sh` para reanudar la prueba. El script
reutiliza la cuenta, el agente y los guardados de `.local/local/`.
Los logs del panel se guardan en `.local/local/coordinator.log`.

## Desplegar en la Raspberry Pi por VPN

1. En la Pi, entra en el clon con tu usuario habitual y ejecuta el instalador.
   Cambia `10.8.0.0/24` por la subred real de los anfitriones de tu VPN si es
   distinta. Puedes consultar la IPv4 de WireGuard con `ip -4 addr show dev wg0`.

   ```sh
   ./scripts/deploy-server.sh --bind 10.8.0.1 --network 10.8.0.0/24
   ```

   Crea la contraseña de `admin` cuando la pida. El script usa `sudo` para
   instalar y habilitar `mcmp-coordinator.service`. El coordinador se ejecuta
   con tu usuario, escucha en la IPv4 indicada y arranca con la Pi. Conserva el
   clon en la misma ruta. La cuenta, la configuración y las copias están en
   `.local/server/data/`. Para comprobar el servicio, usa `systemctl status
   mcmp-coordinator` y `journalctl -u mcmp-coordinator -f`.
   Si la instalación anterior sigue usando el puerto `8443`, puedes elegir
   `--port 8444` y usar ese puerto también en UFW y en las URL del panel.

2. Si UFW bloquea el puerto del panel, permite el acceso TCP por la interfaz
   WireGuard desde la subred de la VPN. Ajusta la interfaz, la subred y el puerto
   del ejemplo para que coincidan con tu instalación.

   ```sh
   sudo ufw allow in on wg0 from 10.8.0.0/24 to 10.8.0.1 port 8443 proto tcp
   ```

3. Copia a cada PC únicamente la CA pública `.local/server/tls/ca.pem` y
   guárdala como `.local/vpn-ca.pem` en el clon del PC. Impórtala como autoridad
   de confianza en el navegador y abre <https://10.8.0.1:8443> con la VPN
   conectada. La importación se hace en el apartado de certificados del
   navegador o del sistema, según el navegador que uses.

4. En el clon de cada PC anfitrión, instala y registra su agente. Usa un código
   distinto de **Registrar un PC Linux** para cada PC.

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   .venv/bin/mcmp agent enroll --state-dir .local/vpn-agent \
     --url https://10.8.0.1:8443 --ca .local/vpn-ca.pem
   .venv/bin/mcmp agent pair --state-dir .local/vpn-agent
   .venv/bin/mcmp agent run --state-dir .local/vpn-agent
   ```

   Introduce el código en **Vincular navegador** y guarda la IPv4 VPN del PC
   en **Red de los anfitriones**. Acepta la EULA en **Ajustes de la partida**.
   Los demás jugadores deben poder alcanzar la IPv4 VPN del anfitrión por el
   puerto de Minecraft configurado en el panel (`25565` de inicio).

## Servicio de usuario Linux

Para que el agente siga activo sin mantener una terminal, crea
`~/.config/systemd/user/mcmp-agent.service` tras registrarlo. Sustituye
las rutas de ejemplo por las absolutas de tu instalación.

```ini
[Unit]
Description=Agente Minecraft Multi-PC

[Service]
ExecStart=/ruta/minecraft-multi-pc/.venv/bin/mcmp agent run --state-dir /ruta/minecraft-multi-pc/.local/agent
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=75

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now mcmp-agent
journalctl --user -u mcmp-agent -f
```

## Copias y recuperación

- El coordinador guarda SQLite y las copias completas bajo `--data-dir`. El
  directorio `snapshots/` conserva los guardados por SHA-256. El panel muestra
  la fecha del último que confirmó. El proceso Java y su mundo de trabajo
  residen en el anfitrión.
- Si el anfitrión pierde la conexión, conserva el mundo y el coordinador
  mantiene la reserva. Si la subida se interrumpe tras un cierre normal, el
  agente puede retomarla al volver la conexión o al reiniciarse.
- Para liberar una reserva interrumpida o sin conexión, verifica en el PC
  anterior que Minecraft está apagado y confirma **Recuperar desde la última
  copia**. El próximo arranque usará esa copia y los cambios hechos después
  pueden perderse. Si todavía no hubo una copia confirmada, se crea un mundo
  nuevo. El directorio `runs/` del agente conserva los archivos locales de una
  ejecución interrumpida, incluido `game/server.log`.
- El MVP transfiere copias completas al parar. Usa espacio temporal tanto en
  el anfitrión como en el coordinador. El catálogo de mundos, los modpacks y el
  agente de Windows pertenecen a fases posteriores.

## Actualizar la instalación de prueba en la Raspberry Pi

Actualiza el coordinador y el agente Linux con el mismo paquete. Desde el panel,
detén Minecraft y espera a que aparezca la nueva fecha en **Última copia guardada**.

1. En este PC, desde la raíz del repositorio, crea el paquete y cópialo a la Pi
   usando la identidad SSH ya guardada y la IP de la VPN.

   ```sh
   agent_base="$PWD/.local/pi-test"
   wheel=minecraft_multi_pc-0.2.0-py3-none-any.whl
   "$agent_base/agent-venv/bin/python" -I -m pip wheel --no-deps --wheel-dir .local/dist .
   scp -o HostName=10.8.0.1 -o HostKeyAlias=192.168.0.200 \
     ".local/dist/$wheel" 192.168.0.200:.local/share/mcmp-pi-test/
   ssh -o HostName=10.8.0.1 -o HostKeyAlias=192.168.0.200 192.168.0.200
   ```

2. En la sesión SSH, comprueba que el PID corresponde al coordinador antes de
   pararlo. Si ya está parado, pasa directamente a copiar el entorno e instalar.

   ```sh
   base="$HOME/.local/share/mcmp-pi-test"
   pid=$(cat "$base/coordinator.pid")
   ps -p "$pid" -o args=
   kill -TERM "$pid"
   ```

   Cuando el proceso haya terminado, guarda una copia del entorno e inicia la
   versión nueva. Si `pip` devuelve un error, usa la copia del entorno para volver
   a la versión anterior antes de seguir.

   ```sh
   backup="$base/venv-$(date +%Y%m%d-%H%M%S)"
   cp -a "$base/venv" "$backup" &&
     "$base/venv/bin/python" -I -m pip install --no-deps \
       "$base/minecraft_multi_pc-0.2.0-py3-none-any.whl" &&
     "$base/start-coordinator"
   printf 'Entorno anterior de la Pi: %s\n' "$backup"
   exit
   ```

3. De vuelta en este PC, verifica el PID del agente y páralo con `kill -TERM`.
   Espera a que se cierre antes de instalar. Si ya está parado, salta ese comando.

   ```sh
   pid=$(cat "$agent_base/agent.pid")
   ps -p "$pid" -o args=
   kill -TERM "$pid"
   ```

   ```sh
   backup="$agent_base/agent-venv-$(date +%Y%m%d-%H%M%S)"
   cp -a "$agent_base/agent-venv" "$backup" &&
     "$agent_base/agent-venv/bin/python" -I -m pip install --no-deps ".local/dist/$wheel" &&
     "$agent_base/start-tunnel" && "$agent_base/start-agent"
   printf 'Entorno anterior del agente: %s\n' "$backup"
   ```

4. Abre <https://127.0.0.1:18443/>, entra con la cuenta actual y comprueba que el
   navegador conserva la vinculación. Inicia la partida, envía `list` y detén
   Minecraft desde el panel. Verifica el registro y la fecha del nuevo guardado.

Para volver a la versión anterior, detén Minecraft y espera a que se guarde.
Detén el coordinador y el agente como en los pasos anteriores. En la Pi,
renombra `venv` a `venv-nuevo` y mueve la copia del entorno a `venv`. En el PC, haz lo mismo con
`agent-venv` y su copia. Arranca el coordinador, el túnel y el agente con los
mismos scripts. La cuenta, las copias y la vinculación se conservan en `data/`
de la Pi y `agent/` del PC, junto con la configuración HTTPS que ya usas.

## Pruebas automatizadas

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -I -m pytest -q
```

[Plan del MVP](docs/plan-mvp.md)

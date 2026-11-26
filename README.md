# Instalación de Minecraft Multi-PC

Usa Linux con Bash y Python 3.12 o posterior. En Ubuntu puedes instalar las
dependencias de los scripts con estos comandos.

```sh
sudo apt update
sudo apt install git python3 python3-venv openssl curl util-linux
```

El PC que ejecuta Minecraft también necesita Java 25 o posterior. En Ubuntu
26.04 puedes instalarlo con `sudo apt install openjdk-25-jre-headless`. Para
jugar, usa Minecraft Java Edition 26.3.

Clona el repositorio en cada equipo donde vayas a instalar el proyecto y
ejecuta los comandos de la guía desde la raíz del clon.

```sh
git clone git@github.com:Rafael-Marquez25/minecraft-multi-pc.git
cd minecraft-multi-pc
```

## Prueba en un PC Linux

1. Ejecuta el script. En la primera ejecución, crea la contraseña de `admin`
   en la terminal y déjala abierta mientras haces la prueba.

   ```sh
   ./scripts/deploy-local.sh
   ```

   Si necesitas otro puerto para el panel, elígelo en la primera ejecución
   con `--port 8444`. Puedes elegir el ejecutable de Java con
   `--java /ruta/a/bin/java`.

2. Importa `.local/local/tls/ca.pem` como autoridad de confianza en el navegador
   y abre <https://127.0.0.1:8443> o la URL que indique el script. Entra como
   `admin`, abre **Registrar un PC Linux**, genera un código y pégalo en la
   terminal cuando lo pida. El código se introduce de forma oculta.
3. Copia el código de vinculación que aparece en la terminal y pégalo en
   **Vincular navegador**. Para generar otro, ejecuta
   `./scripts/deploy-local.sh pair` en una segunda terminal.
4. En **Ajustes de la partida**, acepta la EULA y guarda. Pulsa **Iniciar** y
   conéctate desde Minecraft a la dirección que muestra el panel. Al terminar,
   pulsa **Detener y guardar**, espera a que se confirme la copia y cierra la
   terminal del script con `Ctrl+C`.

Para probar también como `user`, crea la contraseña en **Cuenta user**. Abre
otra sesión en una ventana privada y usa un código de vinculación nuevo.

## Servidor definitivo en la Raspberry Pi

1. Con la VPN operativa, ejecuta el instalador en la Pi como tu usuario
   habitual. Cambia la IPv4 y la subred del ejemplo si tu VPN usa otras. Para
   consultar la IPv4 de WireGuard puedes usar `ip -4 addr show dev wg0`.

   ```sh
   ./scripts/deploy-server.sh --bind 10.8.0.1 --network 10.8.0.0/24
   ```

   Crea la contraseña de `admin` cuando se solicite. El script pide `sudo`
   para instalar y habilitar `mcmp-coordinator.service`. Conserva el clon en
   la misma ruta. El coordinador guarda sus datos en `.local/server/data/`.

2. Si UFW bloquea el panel, abre su puerto TCP en la interfaz VPN. Ajusta
   la interfaz, la subred y el puerto a tu instalación.

   ```sh
   sudo ufw allow in on wg0 from 10.8.0.0/24 to 10.8.0.1 port 8443 proto tcp
   ```

   El script usa el puerto `8443` por defecto. Para una instalación nueva
   puedes indicar `--port 8444` y usar ese mismo puerto en UFW y en las URL.

3. Entrega a los usuarios la URL que imprime el script y la CA pública
   `.local/server/tls/ca.pem`. Desde un equipo conectado a la VPN, importa
   esa CA, abre <https://10.8.0.1:8443> o la URL configurada y entra como
   `admin`. En **Ajustes de la partida**, acepta y guarda la EULA. Si vas a
   dar acceso a la cuenta `user`, crea su contraseña en **Cuenta user**.

Después de iniciar sesión, `admin` y `user` pueden abrir **Este navegador → Certificado HTTPS**
y descargar `minecraft-ca.pem` antes de vincular el PC. Para el primer acceso
desde un navegador que aún no confía en el panel, transfiere la CA por SSH
o desde un equipo de confianza que ya pueda acceder.

### Certificado para Nginx

Si usas un nombre interno como `mc.home.arpa`, añade `--nginx-hostname` al
comando de instalación del servidor. En un despliegue existente, usa la misma
IPv4, subred y puerto que ya tiene el coordinador.

```sh
./scripts/deploy-server.sh --bind 10.8.0.1 --network 10.8.0.0/24 \
  --nginx-hostname mc.home.arpa
```

El instalador firma el certificado con la CA del coordinador y copia
`mc.home.arpa.pem`, `mc.home.arpa.key` y la CA pública `ca.pem` a
`/etc/nginx/minecraft-tls/`. Configura el registro en Dnsmasq y el sitio
HTTPS en Nginx con esos archivos.

Para renovar el certificado, ejecuta el mismo comando cuando queden 30 días o
menos. A continuación, aplica el certificado con
`sudo nginx -t && sudo systemctl reload nginx`.

## PC del usuario en Linux

Estos pasos instalan el agente en el PC que va a alojar la partida.

1. Conecta el PC a la VPN y guarda la CA pública que te dio el administrador
   en `.local/vpn-ca.pem` dentro del clon. Impórtala en el navegador y abre
   la URL del panel. Instala el agente desde la raíz del clon.

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   ```

2. Pide a `admin` un código nuevo de **Registrar un PC Linux**. Ejecuta el
   registro en el PC anfitrión, cambia la URL si corresponde y pega el código
   cuando lo pida.

   ```sh
   .venv/bin/mcmp agent enroll --state-dir .local/vpn-agent \
     --url https://10.8.0.1:8443 --ca .local/vpn-ca.pem
   ```

3. Pide a `admin` que guarde la IPv4 VPN del PC en **Red de los anfitriones**.
   Los jugadores deben poder alcanzar esa IPv4 por el puerto TCP de Minecraft
   que indique el panel, `25565` por defecto.
4. Genera el código en la terminal con `pair`. Entra al panel con la cuenta
   que vayas a usar y pega el código en **Vincular navegador**. Luego ejecuta
   `run` y deja la terminal abierta mientras uses **Iniciar** desde la web.

   ```sh
   .venv/bin/mcmp agent pair --state-dir .local/vpn-agent
   .venv/bin/mcmp agent run --state-dir .local/vpn-agent
   ```

### Servicio de usuario Linux opcional

Para arrancar el agente en segundo plano al iniciar sesión en Linux, crea
`~/.config/systemd/user/mcmp-agent.service` después de registrarlo. Sustituye
`/ruta/minecraft-multi-pc` por la ruta absoluta del clon.

```ini
[Unit]
Description=Agente Minecraft Multi-PC

[Service]
ExecStart="/ruta/minecraft-multi-pc/.venv/bin/python" -I -m mcmp.cli agent run --state-dir "/ruta/minecraft-multi-pc/.local/vpn-agent"
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=75

[Install]
WantedBy=default.target
```

Si tienes el agente en primer plano, detén y guarda la partida desde el panel
y luego cierra el agente antes de activar el servicio.

```sh
systemctl --user daemon-reload
systemctl --user enable --now mcmp-agent
```

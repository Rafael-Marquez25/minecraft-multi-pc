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
   habitual. Los ejemplos usan la IP `10.42.0.1` y una subred `/24`.
   Ajusta ambos valores a tu red. Si tu interfaz WireGuard es `wg0`,
   comprueba la dirección y el prefijo con `ip -4 addr show dev wg0`.

   ```sh
   ./scripts/deploy-server.sh --bind 10.42.0.1 --network 10.42.0.0/24
   ```

   Crea la contraseña de `admin` cuando se solicite. El script pide `sudo`
   para instalar y habilitar `mcmp-coordinator.service`. Conserva el clon en
   la misma ruta. El coordinador guarda sus datos en `.local/server/data/`.

2. Si UFW bloquea el panel, abre su puerto TCP en la interfaz VPN. Ajusta
   la interfaz, la subred y el puerto a tu instalación.

   ```sh
   sudo ufw allow in on wg0 from 10.42.0.0/24 to 10.42.0.1 port 8443 proto tcp
   ```

   El script usa el puerto `8443` por defecto. Para una instalación nueva
   puedes indicar `--port 8444` y usar ese mismo puerto en UFW y en las URL.

3. Para el primer acceso, copia la CA pública `.local/server/tls/ca.pem` por
   SSH o desde un equipo de confianza e impórtala como autoridad en el
   navegador. Con la VPN conectada, abre <https://10.42.0.1:8443> o la URL
   interna configurada en Nginx y entra como
   `admin`. En **Ajustes de la partida**, acepta y guarda la EULA. Si vas a
   dar acceso a la cuenta `user`, crea su contraseña en **Cuenta user**.

Una vez dentro, `admin` y `user` pueden descargar `minecraft-ca.pem` desde
**Este navegador → Certificado HTTPS**, incluso antes de vincular el PC.

### URL interna con Dnsmasq y Nginx

Para servir el panel en <https://mc.home.arpa>, añade `--nginx-hostname`
al comando de instalación. En un despliegue existente, usa el mismo clon,
IP, subred y puerto del coordinador.

```sh
./scripts/deploy-server.sh --bind 10.42.0.1 --network 10.42.0.0/24 \
  --nginx-hostname mc.home.arpa
```

El instalador firma el certificado con la CA del coordinador y copia
`mc.home.arpa.pem`, `mc.home.arpa.key` y la CA pública `ca.pem` a
`/etc/nginx/minecraft-tls/`.

1. Si Dnsmasq todavía no resuelve el nombre, añade el registro en
   `/etc/dnsmasq.d/home-server.conf`. Aplica el cambio con
   `sudo dnsmasq --test && sudo systemctl restart dnsmasq`.

   ```ini
   host-record=mc.home.arpa,10.42.0.1
   ```

2. Crea `/etc/nginx/sites-available/mc.home.arpa`. Si cambiaste el puerto del
   coordinador, usa ese puerto en `proxy_pass`.

   ```nginx
   server {
       listen 10.42.0.1:443 ssl;
       server_name mc.home.arpa;

       ssl_certificate     /etc/nginx/minecraft-tls/mc.home.arpa.pem;
       ssl_certificate_key /etc/nginx/minecraft-tls/mc.home.arpa.key;

       location / {
           proxy_pass https://10.42.0.1:8443;
           proxy_set_header Host $host;
           proxy_ssl_verify on;
           proxy_ssl_name 10.42.0.1;
           proxy_ssl_trusted_certificate /etc/nginx/minecraft-tls/ca.pem;
       }
   }
   ```

3. Habilita el sitio la primera vez y recarga Nginx. Si usas UFW,
   permite también `443/tcp` desde la subred VPN en su interfaz.

   ```sh
   sudo ln -s /etc/nginx/sites-available/mc.home.arpa /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

4. Desde un cliente de la VPN, comprueba que `getent ahostsv4 mc.home.arpa`
   devuelve `10.42.0.1`. Si aún necesitas la CA pública, puedes copiarla desde
   ese cliente con el siguiente comando. Sustituye `tu_usuario` por tu usuario
   SSH del servidor e importa el archivo en el navegador antes de abrir
   <https://mc.home.arpa>.

   ```sh
   scp tu_usuario@10.42.0.1:/etc/nginx/minecraft-tls/ca.pem ./minecraft-ca.pem
   ```

Para renovar el certificado, vuelve a ejecutar el instalador con
`--nginx-hostname` cuando queden 30 días o menos y aplica el cambio con
`sudo nginx -t && sudo systemctl reload nginx`. Los agentes pueden seguir
usando la URL directa del coordinador en `8443`.

### Actualizar un servidor ya desplegado

Si hay una partida activa, pulsa **Detener y guardar** y espera la
confirmación de la copia. Desde el mismo clon, trae los cambios y reinicia
el coordinador con `--update` para instalar el paquete nuevo.

```sh
git pull --ff-only && ./scripts/restart-server.sh --update
```

Para reiniciar solo el coordinador, ejecuta `./scripts/restart-server.sh`.
El script espera a que el panel responda por HTTPS. Añade `--reload-nginx`
si también quieres validar y recargar la configuración de Nginx.

Si vas a habilitar Nginx por primera vez o a renovar su certificado, ejecuta
también el instalador con `--nginx-hostname`. El estado del coordinador queda
en `.local/server/data/`. Al pasar de la IP al dominio, inicia sesión y vuelve
a vincular el navegador una vez, porque las cookies se guardan por host.

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
     --url https://10.42.0.1:8443 --ca .local/vpn-ca.pem
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

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
El agente usa ese PEM para conectarse al coordinador por la VPN. Si el
navegador también usa la CA privada, importa el mismo archivo.

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

### Acceso por VPN con un certificado público

Si controlas un subdominio como `mc.example.net`, puedes abrir el panel en un
navegador nuevo sin importar la CA. Mantén Nginx en la IP de la VPN y el
coordinador con su CA y su puerto habituales.

1. En Dnsmasq, configura `host-record=mc.example.net,10.42.0.1`. El nombre
   debe resolver a la IP privada desde los clientes de la VPN.
2. Emite el certificado de `mc.example.net` con Let's Encrypt mediante
   **DNS-01**. Configura el complemento DNS de Certbot para tu proveedor
   y un nombre de certificado igual al subdominio. La validación usa un
   registro TXT público y el panel permanece accesible solo por la VPN.
   [Validación DNS-01](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge) ·
   [Complementos DNS de Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins)
3. En el bloque de Nginx de la sección anterior, cambia `server_name` al
   subdominio y usa estos archivos para el TLS del navegador. Mantén
   `listen` en la IP VPN y conserva las directivas HTTPS del upstream,
   incluida `proxy_ssl_trusted_certificate` con la CA privada.

   ```nginx
   server_name mc.example.net;
   ssl_certificate     /etc/letsencrypt/live/mc.example.net/fullchain.pem;
   ssl_certificate_key /etc/letsencrypt/live/mc.example.net/privkey.pem;
   ```

   Para preparar el archivo de confianza del upstream en un despliegue nuevo,
   ejecuta desde el clon del servidor:

   ```sh
   sudo install -D -m 0644 .local/server/tls/ca.pem /etc/nginx/minecraft-tls/ca.pem
   ```

   Valida y recarga con `sudo nginx -t && sudo systemctl reload nginx`.
4. Habilita la renovación automática de Certbot y prueba el desafío con
   `sudo certbot renew --cert-name mc.example.net --dry-run`. Configura
   un *deploy hook* ejecutable en `/etc/letsencrypt/renewal-hooks/deploy/`
   que ejecute `nginx -t && systemctl reload nginx` después de una renovación.
   [Renovación en Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)
5. Comprueba desde un cliente de la VPN que el panel abre sin importar la
   CA y que puedes descargar `minecraft-ca.pem` para el agente. Comprueba
   también que el panel no responde desde fuera de la VPN. Cada navegador
   debe iniciar sesión y vincularse una vez en el dominio nuevo.

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

Para preparar o renovar el certificado privado de Nginx, ejecuta también
el instalador con `--nginx-hostname`. Si usas un certificado público,
renuévalo con Certbot. El estado del coordinador queda en
`.local/server/data/`. Al pasar de la IP al dominio, inicia sesión y vuelve
a vincular el navegador una vez, porque las cookies se guardan por host.

## PC del usuario en Linux

El script instala y arranca el agente en el PC que va a alojar la partida y
lo conecta al coordinador que ya está funcionando en el servidor.

1. Conecta el PC a la VPN, entra al panel como `user` o `admin` y descarga
   `minecraft-ca.pem` desde **Este navegador → Certificado HTTPS**. Si el
   navegador usa la CA privada y aún no confía en ella, obtén la primera
   copia del administrador y luego impórtala.
2. Desde la raíz del clon, ejecuta lo siguiente con la URL VPN **directa**
   del coordinador. Ajusta la IP, el puerto y la ruta al PEM si difieren.

   ```sh
   ./scripts/deploy-agent.sh --url https://10.42.0.1:8443 --ca ~/Descargas/minecraft-ca.pem
   ```

   Si guardaste el PEM en `.local/vpn-ca.pem`, puedes omitir `--ca`.
3. Cuando el script lo pida, genera un código en **Registrar un PC Linux**
   y pégalo en la terminal. El coordinador guarda la IPv4 VPN desde la que
   recibe el registro. Los jugadores deben poder alcanzar esa IP por el
   puerto TCP de Minecraft, `25565` por defecto.
4. Pega el código que muestra el script en **Vincular navegador**. Deja la
   terminal abierta y pulsa **Iniciar** en el panel. Si el código de
   vinculación caduca, ejecuta `./scripts/deploy-agent.sh pair` en otra
   terminal. Si caduca el de registro, genera uno nuevo en el panel y
   vuelve a ejecutar el comando de alta.

Para un alta manual, `admin` puede marcar **Configurar la IPv4 VPN
manualmente** al generar el código y guardar luego la IP en **Red de los
anfitriones**.

Para los siguientes arranques ejecuta `./scripts/deploy-agent.sh`. El script
reutiliza el registro de `.local/vpn-agent/`, incluido el que hayas hecho
manualmente en ese directorio. Si necesitas elegir el ejecutable de Java,
añade `--java /ruta/a/bin/java` al comando de arranque.

### Servicio de usuario Linux opcional

Para arrancar el agente en segundo plano al iniciar sesión en Linux, crea
`~/.config/systemd/user/mcmp-agent.service` después de registrarlo. Sustituye
`/ruta/minecraft-multi-pc` por la ruta absoluta del clon.

```ini
[Unit]
Description=Agente Minecraft Multi-PC

[Service]
ExecStart="/ruta/minecraft-multi-pc/.local/vpn-agent/venv/bin/python" -I -m mcmp.cli agent run --state-dir "/ruta/minecraft-multi-pc/.local/vpn-agent"
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

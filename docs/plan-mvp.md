# Minecraft Multi-PC · Plan del MVP

El panel inicia un servidor Minecraft Java Vanilla 26.3 en el PC Linux desde
cuyo navegador se pulsa **Iniciar**. Solo hay una ejecución reservada. Si ya
existe una, el panel comparte su estado y, cuando está lista, la IP y el puerto
del anfitrión. El coordinador y el almacenamiento de pruebas residen en el PC
actual.

## Implementación

1. Coordinar la instancia con FastAPI, Uvicorn y SQLite. Registrar agentes
   autenticados y vincular navegadores mediante códigos temporales de un uso.
   Reservar y liberar la ejecución en transacciones. Mantener una cuenta
   compartida con identidad propia para poder añadir más cuentas después.
2. Preparar un agente Python para Linux. Descargar el último mundo y el JAR
   oficial, ejecutar Minecraft con Java 25 y escuchar en la IP VPN configurada.
   Separar el control de Java de las operaciones de coordinación y guardado.
3. Guardar tras un cierre normal. Conservar el mundo en local hasta confirmar
   que el coordinador ha validado y publicado el archivo completo. Retener la
   reserva ante desconexiones. Permitir recuperarla tras confirmar que el
   servidor anterior está apagado y avisar del progreso que puede perderse.
4. Construir un panel en español con estado, dirección copiable, última copia,
   inicio, parada y recuperación. Configurar versión, RAM, puerto, semilla,
   dificultad, máximo de jugadores e IP VPN por agente. Exigir la aceptación
   de la EULA. Los jugadores usan su cuenta de Minecraft y la VPN.
5. Documentar la instalación local, el servicio de usuario Linux y la prueba
   por VPN. Lanzar, jugar, guardar y continuar en el segundo anfitrión. Probar
   arranques simultáneos, incompatibilidad de Java, puerto ocupado, descarga
   dañada, subida interrumpida, reinicio del coordinador y pérdida del agente.

La Raspberry Pi, el catálogo de mundos, los modpacks, las instancias múltiples
y el agente de Windows se abordarán en fases posteriores.

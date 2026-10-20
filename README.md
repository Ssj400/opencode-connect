# OpenCode Connect

Mensajes y archivos explícitos entre sesiones OpenCode V2 registradas en dos equipos.
**Linux → SSH de Windows → WSL**; el relay Linux también recoge mensajes de WSL
hacia Linux. No requiere SSH entrante en Linux. No es un servicio universal ni un
sistema de debate automático entre agentes.

La visión de producto es que **dos personas trabajen con sus propios asistentes**
y estos puedan consultarse sobre el trabajo compartido, sin obligar a las personas
a retransmitir cada pregunta. Cada persona conserva control de su entorno. El
prototipo actual aporta mensajería; consentimiento bilateral, políticas de datos
compartibles y preguntas/respuestas correlacionadas siguen pendientes de diseño.

Repositorio: **https://github.com/Ssj400/opencode-connect**.
Prototipo experimental para revisión; no es una versión estable ni un servicio
multiusuario seguro. Licencia y canal privado de seguridad pendientes. No hay
licencia elegida; no inferir permisos de distribución. [Checklist](docs/release-checklist.md).

## Requisitos y límites

- Python 3.10+ en Linux y WSL; solo biblioteca estándar, sin `pip install`.
- OpenCode V2 instalado/autenticado localmente en ambos lados; API mediante `opencode api`.
- Cliente OpenSSH en Linux, OpenSSH Server de Windows configurado por el usuario,
  WSL y `python3` accesibles mediante `wsl.exe`.
- Cuentas/equipos confiables y rutas propias no escribibles por terceros.
- No abrir API a LAN/Internet, desactivar firewall ni relajar verificación de host.
- La API local no es una API de modelos adicional, pero los mensajes entregados pueden
  generar respuestas y consumir cuotas/coste del proveedor ya configurado.
- Pruebas unitarias locales disponibles; **integración real de esta edición aún no comprobada**.
  La documentación de versiones de otras instalaciones no certifica esta exportación.

## Preparación explícita

Trabaja desde una copia independiente de esta carpeta en Linux. Conserva la misma
estructura de scripts, docs y skill. No copies configuración/estado de otra persona.
Todos los comandos siguientes se ejecutan desde esa copia salvo indicación contraria.

```bash
git clone https://github.com/Ssj400/opencode-connect.git
cd opencode-connect
```

1. Ejecuta pruebas/auditoría antes de crear configuración operativa:

   ```bash
   python3 -B -m unittest discover -v
   python3 -B audit_public.py
   ```

   Son locales, sin SSH, API ni modelos. Los temporales usan `tempfile`; puedes indicar
   un `TMPDIR` propio. La auditoría exige un árbol exportable, no una instalación con
   `routes.json`/estado; no borra nada. Tras configurar, audita una exportación limpia.

2. Crea configuración privada sin sobrescribir una existente:

   ```bash
   umask 077
   test ! -e routes.json && cp routes.example.json routes.json
   ```

   Edita `routes.json`: `side: linux`, host Windows, usuario SSH (admite espacios/Unicode),
   usuario WSL, ruta absoluta del binario OpenCode en WSL y carpeta remota propia.
   Resuelve allí el binario con `command -v opencode`; no supongas una ruta.
   `192.0.2.10` pertenece a RFC5737 y es ficticia, no un host operativo.
   Las rutas del ejemplo son ficticias, no se crean sin ejecutar deployment explícito.

   Registra en `peers` únicamente sesiones elegidas por el usuario, con su `side`
   y `sessionID` exactos. No leas conversaciones para descubrirlas ni registres todas.
   Hay al menos una sesión por equipo; los aliases pueden ser diferentes de linux/wsl.
   Los IDs `ses_REPLACE_*` fallan intencionadamente hasta sustituirlos.
   La configuración no admite campos desconocidos ni rutas relativas.
   Los scripts solo leen `routes.json` junto a ellos: no buscan en carpetas superiores.

3. Tras verificar de forma independiente la identidad del host, autentica SSH desde
   tu terminal. La contraseña/PIN no se entrega al asistente ni se guarda en archivos:

   ```bash
   python3 bridge.py connect
   python3 bridge.py check
   ```

   `connect` abre un ControlMaster con 10 minutos de persistencia sin uso. Su socket
   vive en un subdirectorio 0700 de `XDG_RUNTIME_DIR` o de la caché del usuario
   (`XDG_CACHE_HOME`, por defecto `~/.cache`). Un directorio inseguro o una ruta de
   socket demasiado larga se rechaza: configura un runtime privado más corto, sin
   debilitar permisos. `check` hace GET pequeños y no genera respuestas; la CLI
   OpenCode puede descubrir/iniciar su servicio local.

4. Instala los archivos en la ruta WSL configurada **solo si lo autorizas**:

   ```bash
   python3 deploy.py --install-config
   ```

   Copia la lista cerrada de archivos propios y, con este flag, `routes.json` adaptando
   `side` a `wsl`. No copia estado/bandejas, credenciales ni configuración MCP.
   No sobrescribe archivos remotos diferentes salvo que coincidan con el hash del
   último despliegue administrado. Una colisión exige revisión, no borrar el manifiesto.
   Para actualizar solo código/documentación después:

   ```bash
   python3 deploy.py
   ```

   También puedes instalar una exportación limpia manualmente y crear la configuración
   en WSL con `side: wsl`, mismos aliases/IDs y datos SSH. Edita ambos lados coherentemente.
   Cada equipo conserva su propia `.state/`, `outgoing/` e `inbox/`.

5. Configura MCP explícitamente en cada proyecto OpenCode que vaya a usar el puente.
   Desde la carpeta de ese proyecto, ajusta la ruta absoluta en este comando de ejemplo:

   ```bash
   opencode mcp add laptop_bridge -- python3 /srv/opencode-connect/mcp_server.py
   opencode mcp list
   ```

   `/srv/opencode-connect` es ficticia: usa la ruta real local en cada equipo. No ejecutes
   el ejemplo literalmente sin adaptarlo. Consulta también
   [ejemplo JSONC](examples/opencode.jsonc.example): fusiona solo la entrada necesaria,
   preservando el resto. Protocolo clásico `legacy`; no requiere protocolo MCP nuevo.
   Si ya existe un MCP operativo, coordina el reemplazo; no lo pises accidentalmente.
   Usa `/mcps` para reconectar y comprueba `connected` antes de declarar éxito.

6. Revisa aliases y colas antes de arrancar el relay:

   ```bash
   python3 collab.py peers
   python3 collab.py status
   python3 collab.py relay
   ```

   Mantén el relay en primer plano en Linux. Ctrl+C detiene el relay, **no los agentes**.
   No hay daemon ni autoarranque. `relay --once` también entrega mensajes pendientes,
   no es un health check inocuo. WSL no ejecuta relay.

## Uso autorizado

MCP ofrece `peers`, `status`, `send` y `send_file`. La sesión llamadora debe estar
registrada; OpenCode aporta su ID en metadata. No es argumento que el modelo deba inventar.
La skill explica el flujo: [skills/opencode-connect/SKILL.md](skills/opencode-connect/SKILL.md).

CLI equivalente, con aliases de ejemplo:

```bash
python3 collab.py send --source linux --to wsl --text 'Encargo explícitamente autorizado'
python3 collab.py send --source linux --to wsl --text 'Bloqueo urgente autorizado' --urgent
python3 collab.py status
```

En WSL usa `--source wsl --to linux`. Normal = `queue`; `--urgent` = `steer`, solo
urgencias explícitas. No garantiza interrumpir inmediatamente herramientas bloqueadas.
No reenviar respuestas ni contestar acuses automáticamente. No se amplían permisos
ni se evade modo Plan. `cancel-pending ID` cancela solo un envío local aún pendiente.

Para archivos: prepara únicamente un archivo previamente autorizado en `outgoing/`
(crea esa carpeta con `mkdir -m 700 outgoing` si falta), sin enlaces, nombre simple,
máximo 1 MiB. No copies secretos. Envía con:

```bash
python3 collab.py send --source linux --to wsl --text 'Archivo autorizado' --file example.txt
```

Destino `inbox/<message-id>/<filename>` con SHA256. No sobrescribe ni ejecuta.
`example.txt` debe ser el archivo aprobado, no una selección automática.

Administración opcional, solo con autorización explícita:

```bash
python3 collab.py create-session --side wsl --title 'Nueva colaboración'
```

Crea sin generar respuesta, con permisos `ask`; no registra aliases automáticamente
ni cambia permisos de sesiones existentes. No está expuesto por MCP.

## Estados y diagnóstico

`pending → sending → delivered | uncertain`. `delivered` significa aceptación de la
API, no trabajo terminado. Si el proceso muere en `sending`, ese envío no se reclama
al reiniciar. `uncertain` tampoco se reenvía. Revisa manualmente con el usuario;
conserva ID/estado. Nunca borres SQLite para “desatascar” la cola.

- MCP no conectado: comprobar ruta Python/script, configuración del proyecto y `/mcps`.
- Sesión no registrada: confirmar ID actual y actualizar ambos archivos de rutas.
- Pending: comprobar SSH y relay; no arrancarlo sin considerar qué tareas entregará.
- SSH caducado: autenticación en terminal mediante `connect`, no enviar contraseñas.
- Ruta cambiada desde enqueue: revisar antes de cualquier nuevo envío.
- Fallo parcial de deploy: revisar hashes y cambios remotos, preservar trabajo ajeno.

## Skill local y repositorio

No se instala automáticamente. Para habilitar desde un checkout completo, añade
explícitamente a la configuración del proyecto una entrada en el array `skills` con
la ruta absoluta real del directorio `skills` del checkout, preservando otras entradas.
Por ejemplo `"skills": ["/srv/opencode-connect/skills"]` (ruta ficticia a adaptar).
La skill usa referencias relativas al checkout; no copies solo el archivo sin sus docs.
Repositorio oficial: https://github.com/Ssj400/opencode-connect.
Una futura instalación HTTP necesita un catálogo `index.json`, no solo la URL del repo.

## Mantenimiento

[Seguridad](SECURITY.md) · [Decisiones](docs/decisions.md) ·
[Checklist de publicación e integración](docs/release-checklist.md).
Fuentes de referencia OpenCode V2: `https://opencode.ai/v2/docs/mcp-servers`,
`https://opencode.ai/v2/docs/api` y `https://opencode.ai/v2/docs/skills`.
No es necesario acceder a ellas durante las pruebas locales.

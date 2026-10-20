# Seguridad y límites

## Modelo de confianza

Herramienta personal para dos equipos y cuentas de sistema confiables: coordinador
Linux → SSH de Windows → WSL. MCP usa stdio; no abre un puerto adicional.
La API de OpenCode permanece local y se accede mediante `opencode api`.
La metadata `ai.opencode/sessionID` es correlación, no autenticación. La allowlist
reduce errores de encaminamiento; no es aislamiento frente a un usuario local hostil.
Quien puede editar scripts, configuración, estado o comandos SSH ya controla el puente.
No ofrecer este RPC ni MCP como servicio público o multiusuario.

## Garantías implementadas y límites

- Solo aliases registrados; rutas y usuarios se validan antes del transporte.
- JSON por stdin; mensajes y rutas no se interpolan en comandos del shell Windows.
- Sin desactivar comprobación de host ni copiar credenciales internas de OpenCode.
- Socket de control en directorio privado del usuario, no en un directorio temporal compartido.
- SQLite y bandejas son datos privados. El entorno y rutas de instalación deben ser
  propiedad del usuario; no permitir escrituras de terceros.
- El archivo se limita a 1 MiB, se verifica por SHA256 y se crea sin sobrescribir.
  El hash verifica integridad, no confianza ni autenticidad del contenido.
- Las comprobaciones de symlinks no son una defensa completa frente a carreras
  provocadas por procesos maliciosos de la misma cuenta. No compartir esta instalación.
- La denegación de algunos nombres de credenciales es solo defensa adicional:
  no detecta secretos dentro de un archivo con nombre inocuo. Revisar contenido manualmente.
- Límite de 30 envíos/hora por equipo; no es una garantía de gasto máximo.
- Un POST ambiguo no se reintenta. No se promete entrega exactamente una vez.
  Puede quedar un archivo recibido sin su notificación si el POST posterior falla.
- No hay reconciliación automática de `sending`/`uncertain` ni cancelación de agentes.
- Los permisos de las sesiones registradas siguen vigentes. `create-session` pide
  permisos `ask`, no habilita herramientas automáticamente ni inicia una generación.
- Deployment comprueba cambios remotos mediante manifiesto y hashes; archivos se
  escriben atómicamente por separado, no como transacción del árbol completo.
  Un fallo parcial puede requerir revisión manual. Nunca borrar el manifiesto para forzar.

## Datos que no se publican

`routes.json`, `.state/`, bandejas, sockets, logs, backups, tokens, identidades,
IPs operativas, huellas SSH, IDs reales de sesiones o mensajes e informes de ejecución.
`audit_public.py` aplica una allowlist y heurísticas; no sustituye revisión humana ni
un escáner de secretos. No usarlo como certificación de ausencia de datos personales.

## Incidentes

Detener el relay y desconectar el MCP afectado sin cancelar agentes. Preservar estado,
revisar destinos y mensajes antes de cualquier reenvío. No adjuntar SQLite ni logs sin
saneamiento. Canal privado para reportar vulnerabilidades: pendiente de definir con
el owner antes de una versión estable; no abrir issues públicos con secretos.
El repositorio inicial es un prototipo experimental, no una oferta de servicio.

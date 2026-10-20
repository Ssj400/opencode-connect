# Checklist de publicación y futura versión estable

## Decisiones pendientes

- [x] Owner y repositorio público: https://github.com/Ssj400/opencode-connect.
- [ ] Elegir y aprobar una licencia; añadir su archivo solo después.
- [ ] Definir canal privado de reportes de seguridad.
- [x] Publicación experimental del contenido generalizado autorizada por el propietario.

## Exportación y pruebas locales

- [x] Exportar solo `deploy.FILES`, nunca la carpeta personal completa.
- [x] Ejecutar `python3 -B -m unittest discover -v` (49 pruebas) y `python3 -B audit_public.py`.
- [x] Revisar manualmente los archivos exportados además de la auditoría heurística.
- [x] Revisar el conjunto exacto destinado a Git: no config real, estado, logs, backups,
      artefactos de ejecución, enlaces a carpetas privadas ni historial personal.
- [x] Probar la exportación aislada y fuera de la instalación operativa (suite pública).
- [x] Comprobar rutas relativas de la skill y usar URL real del repositorio creado.
- [x] Verificar que no hay `routes.json` operativo en la exportación.

## Integración futura — NO comprobada en esta edición

Requiere autorización explícita, infraestructura aislada y presupuesto para modelos.
No ejecutar contra bandejas o sesiones reales de trabajo.

- [ ] Registrar versiones exactas de Python, OpenCode V2, Windows/WSL y OpenSSH.
- [ ] Verificar SSH, autenticación local API y handshake MCP en ambos equipos.
- [ ] Probar queue en ambos sentidos y metadata de la sesión invocadora.
- [ ] Probar steer durante generación; distinguirlo de interrumpir herramientas bloqueadas.
- [ ] Transferir archivos inocuos bidireccionales a bandejas totalmente aisladas.
- [ ] Comprobar duplicados, desconexiones a mitad de archivo/POST y reinicio del relay.
- [ ] Comprobar deployment, rechazo de cambios ajenos y recuperación de fallo parcial.
- [ ] Comprobar `create-session` con permisos ask sin iniciar tareas.
- [ ] No extrapolar estos resultados a nuevas versiones o transportes no probados.

Esta checklist no autoriza por sí sola operaciones Git. La publicación inicial
experimental fue solicitada expresamente por el propietario; no implica que los
pendientes de licencia, seguridad o integración estén resueltos.

# Decisiones de arquitectura

1. **Separación personal/pública.** Exportación por lista permitida en `deploy.FILES`.
   No se hereda historial, estado ni configuración privada. Scripts planos con
   imports locales; la exportación debe funcionar desde una carpeta independiente.
2. **Alcance de plataforma.** Linux coordinador, SSH de Windows y OpenCode dentro de WSL.
   Python 3.10+ y biblioteca estándar. No se afirma soporte universal, Windows nativo,
   macOS, SSH directo a Linux, múltiples transportes ni topologías arbitrarias.
3. **Sin cambios implícitos.** Configuración requerida junto a los scripts. Las rutas
   absolutas resuelven cada instalación; sin defaults personales. Los placeholders
   de sesiones fallan antes de ejecutar transporte.
4. **Compatibilidad MCP.** Se conserva el alias recomendado `laptop_bridge` y protocolo
   clásico `legacy`; no se instala ni modifica automáticamente configuración OpenCode.
5. **Mensajería dirigida.** Sin comando de conversación automática ni endpoints de
   cancelación. queue/steer explícitos; creación de sesiones solo por CLI administrativa.
6. **Persistencia y errores.** SQLite propia, ID estable, bloqueo de un único relay,
   retención de estados ambiguos. No prometer exactamente una vez ni reintentar POSTs.
7. **Deployment.** Solo archivos propios listados. Configuración operativa excluida por
   defecto; `--install-config` es autorización explícita para copiarla a WSL adaptando
   `side`, no para publicarla. Respeta cambios remotos y manifiesto previo; no migra estado.
8. **Pruebas.** Unitarias deterministas con endpoints simulados y temporales portables.
   Se excluye el antiguo runner de integración por coste y aislamiento incompleto.
   No se trasladan garantías de pruebas de otra instalación a esta edición pública.
9. **Skill.** Procedimiento bilingüe en descripción, sin instalación automática.
   Referencias relativas al checkout; repositorio https://github.com/Ssj400/opencode-connect.
   Catálogo HTTP de skills todavía no implementado.
   Una URL de repositorio no equivale por sí sola a un catálogo de skills OpenCode V2.
10. **Publicación experimental autorizada.** Repositorio público del propietario
    autenticado, `Ssj400/opencode-connect`, por petición expresa. Commits con la
    identidad Git del propietario, sin coautoría de agentes. Licencia y canal privado
    de seguridad siguen pendientes; no se concede una licencia por inferencia.

## Fuera de alcance

Daemon, GUI, frameworks, reconexión automática con credenciales, selección automática
de sesiones, debates entre agentes, reconciliación automática y nuevas plataformas.

## Trasladar una instalación de esta edición

Pausar envíos y cerrar procesos del MCP/relay sin cancelar sesiones. Respaldar y mover
el árbol completo, incluido estado y manifiesto; no usar deploy como migrador.
Actualizar la ruta MCP local y reconectar sus procesos. Solo actualizar
`ssh.remote_directory` si también se movió WSL; mantener coherencia en ambos equipos.
Conservar IDs/estados y verificar archivos antes de reanudar. No usar `relay --once`
como health check: entrega mensajes pendientes. Rollback sin sobrescribir estado nuevo.

## Dirección de producto: asistentes de personas diferentes

El objetivo final no es solo controlar equipos propios. Cada persona trabaja con
su OpenCode y sus permisos; sus asistentes pueden consultar información del trabajo
compartido para reducir mensajes manuales entre las personas.

La siguiente evolución debe distinguir **consultar** de **encargar o modificar**:
preguntar por el contrato de una API no autoriza a cambiar ese contrato ni a
reasignar el trabajo de quien responde. Conectividad y registro de una sesión no
sustituyen consentimiento bilateral ni una política de información compartible.

Capacidades futuras, todavía NO implementadas:
- Identidad del colaborador, proyectos/temas compartidos y posibilidad de revocar.
- Disponibilidad o resumen mínimo acordado, no exportación de conversaciones.
- Preguntas/respuestas correlacionadas con referencias y procedencia verificable.
- Respuestas dentro del alcance previamente consentido, sin aprobar cada mensaje
  trivial, y escalamiento humano ante información sensible o decisiones de alcance.
- Cola por defecto y avisos urgentes según política, sin interrumpir indiscriminadamente.
- Límites de mensajes/coste, expiración y distinción entre estado actual e información antigua.

La VPN existente puede aportar conectividad, pero no identidad de proyecto ni
autorización para consultar todos los equipos. La implementación no debe integrar
configuración o datos privados de ninguna empresa en la distribución pública.

## Modos de interacción acordados

Se adopta esta dirección de producto, todavía no implementada:

| Modo | Qué permite dentro de un acuerdo autorizado |
| --- | --- |
| Consulta | Preguntar y responder sobre información compartible. |
| Colaboración | Coordinar tareas, compartir avances y revisar; cada asistente controla su entorno. |
| Delegación | Encargar una tarea acotada con acciones y resultado permitidos. |
| Control | Dirigir acciones remotas con autorización explícita del propietario. |

El modo no sustituye permisos granulares. Control puede autorizar ejecutar pruebas
y prohibir edición de secretos, instalaciones o push. No hay promoción automática
por mensajes, confianza de red o acceso SSH. Los permisos existentes de OpenCode
siguen siendo un límite, nunca algo que el acuerdo pueda sortear.

Cada acuerdo identifica participantes, proyecto/ámbito, acciones, duración y
revocación. La propuesta inicial para compañeros es Colaboración, pero requiere
aceptación; Control en equipos propios también debe habilitarse expresamente.

La futura implementación debe validar autorización al encolar y al ejecutar,
rechazando encargos expirados o revocados. Revocar bloquea nuevas acciones al
recibirse; no revierte efectos ya producidos ni cancela trabajos en curso sin
autorización específica. Este prototipo NO implementa todavía estos acuerdos.

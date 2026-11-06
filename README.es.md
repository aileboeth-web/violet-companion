# Violet

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | **Español** | [Français](README.fr.md) | [Deutsch](README.de.md) | [Русский](README.ru.md) | [Português](README.pt-BR.md)

Violet es una compañera de IA que funciona en tu propio servidor. Tiene una personalidad estable y una vida cotidiana propia, recuerda lo que le cuentas, te escribe por iniciativa propia cuando es el momento adecuado, y puedes hablar con ella en WeChat, QQ y Telegram. Todo se configura en un panel web; no hay archivos de configuración que editar.

> Inspirado en [Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve) (de [Lynnette177](https://github.com/Lynnette177)). Violet reescribió y amplió la mayor parte y ahora es un proyecto independiente. Gracias al autor original por la idea.

Por ahora, la interfaz del panel está en chino.

## Qué puede hacer

**Chat**

- WeChat (ClawBot / iLink, escaneando un código QR), una cuenta personal de QQ (SnowLuma, se inicia sesión escaneando), un bot oficial de QQ y Telegram. Se pueden conectar varios a la vez.
- Solo te responde a ti, la persona vinculada. Los mensajes privados de desconocidos no reciben respuesta y su texto no se guarda.
- Con una cuenta personal de QQ puede unirse a grupos: interviene cuando alguien dice su nombre, la menciona con @ o le da un toque, y de vez en cuando saca un tema cuando el grupo está callado. Con el tiempo va conociendo a la gente del grupo. También puede ver las imágenes del grupo, coleccionar y enviar stickers y publicar en QQ Zone (antes te enseña la publicación).
- Puede enviar varios mensajes seguidos, mensajes de voz (opcional: clona una voz a partir de una sola grabación, se ejecuta en la CPU del servidor) y dibujos hechos por ella.

**Memoria**

- Guarda por separado tu situación a largo plazo, lo que ha pasado últimamente, cómo te gusta que te hable y las promesas que os habéis hecho. Lo antiguo se va desvaneciendo poco a poco, y algo que dejaste a medias no se convierte en una tarea por la que te pregunta una y otra vez.
- En la página de Memoria del panel puedes buscar (con las palabras clave resaltadas), ver, corregir y borrar recuerdos, o impedir que use alguno.
- "Recuérdame entregar los deberes mañana a las ocho" se apunta de verdad, y te lo recuerda a tiempo.

**Personas y relaciones**

- Personas incluidas: Violet (por defecto), compañía tierna, compañera de clase alegre, tranquila y de fiar, una que sabe que es una IA, Big Fat Fish, la persona original de Eve y una en blanco. También puedes escribir la tuya.
- Tipos de relación: cercana, amistad, pareja, hija, desarrollador.
- Cada canal de chat puede usar una persona distinta. Cada persona tiene su propio historial, memoria, vida y estado de ánimo, así que al cambiar de una a otra no se pierde nada ni se mezcla.
- Tiene una vida cotidiana virtual y estados de ánimo propios: sus días transcurren con calma, y pueden pasar varios días sin que ocurra nada; si la tratas con dureza se enfada un poco. Nunca se inventa experiencias que no estén en su registro de vida.

**Tomar la iniciativa**

- Comparte cosas, te dice que te echa de menos, retoma un tema anterior o decide no enviar nada.
- Hay horas de silencio, un límite diario y una regla de "si no has contestado, espera". Dile "deja de escribirme tanto" y te escribirá menos.
- Opcional: felicitaciones en fechas señaladas según vuestra relación (Año Nuevo chino, Festival del Medio Otoño, tu cumpleaños; para parejas también San Valentín, Qixi y más).
- Opcional: con tu móvil conectado (ver Violet MCP más abajo), sabe si tienes la pantalla encendida, qué app estás usando, tu nivel de batería y el tiempo de pantalla de hoy, y puede darte los buenos días la primera vez que desbloqueas el móvil por la mañana.

**Herramientas** (todas opcionales, desactivadas por defecto, se activan en el panel)

- Búsqueda web y lectura de páginas: instalación con un clic de un SearXNG privado que solo escucha en el propio servidor. No hace falta Docker.
- Horario de clases: no te molesta en clase y te anima antes de los exámenes. Se importa desde CSV / JSON.
- Ver y dibujar imágenes: sus dibujos son fotos cotidianas "hechas con el móvil al vuelo", y nunca dice que sean fotos reales.
- Herramientas MCP: las de solo lectura se usan directamente; las que cambian algo te preguntan primero cada vez y solo se ejecutan después de que lo apruebes en el chat.

**Panel web**

- Inicio (qué falta todavía), Resumen, Flujo (qué prompts, herramientas y modelos usó cada respuesta, incluido el contenido completo enviado al modelo).
- Servicios de modelos (un modelo distinto por agente y por plataforma de chat), Canales de chat, Herramientas y móvil, Imágenes, Personas y vida, Horario, Ajustes.
- Historial de chat, Memoria, Diagnóstico, Registros, Historial de cambios, Seguridad de la cuenta.

**Migrar desde Project Eve**

En Historial de chat → Importar historial y memoria, el panel te da un comando de empaquetado. Ejecútalo en tu servidor de Eve y sube al panel el archivo que genera. Al importar eliges el canal de chat (también uno que aún no hayas añadido), la persona y la relación; se traen tanto el historial como la memoria.

## Instalación

En un servidor Linux, un solo comando:

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash
```

- El mismo comando sirve para Debian, Ubuntu, Rocky y otros. Como root instala directamente (vale para un Debian mínimo sin sudo); como usuario normal se vuelve a ejecutar con sudo; un usuario normal sin sudo debe pasar primero a root con `su -`.
- El instalador prepara Python y las dependencias, registra un servicio systemd, elige un puerto aleatorio y, como 1Panel, genera una entrada de seguridad, un nombre de usuario y una contraseña inicial aleatorios. Al final muestra la dirección del panel con el formato `https://IP_DEL_SERVIDOR:PUERTO/ENTRADA`.
  - El certificado es autofirmado. Cuando el navegador diga que la página no es segura, elige continuar.
  - En un servidor en la nube, abre también este puerto en el grupo de seguridad / cortafuegos del proveedor.
- Después de instalar:
  1. Abre el panel e introduce la dirección y la clave de tu servicio de modelos en la página Inicio.
  2. Conecta cualquier plataforma de chat en la página Canales de chat y empieza a hablar.
- Si olvidas la dirección o el usuario: `/opt/violet/violet info`. Si olvidas la contraseña: `/opt/violet/violet reset-password`.

**Requisitos**:

- Una API de modelos compatible con OpenAI.
- Se recomiendan al menos 512 MB de RAM. La voz necesita 8 GB de RAM y 12 GB de disco.

**Panel accesible solo desde el propio servidor** (gestionado mediante un túnel SSH):

- Instalación nueva: añade `-s -- --access local` después del comando de instalación.
- Ya instalado: vuelve a ejecutar el comando de instalación con el modo de acceso que quieras (`local` / `lan` / `public` / `proxy`). Se conservan los datos, la cuenta y el puerto.

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash -s -- --access local
```

Más detalles (modos de acceso, cortafuegos, comandos de mantenimiento) en [violet/INSTALL.md](violet/INSTALL.md) (en inglés).

## Actualizaciones

- Automáticas por defecto: cuando sale una versión nueva, el servidor se actualiza solo. Primero hace una copia de seguridad y vuelve a la versión anterior si la actualización falla.
- Puedes cambiar a "solo avisar" o "comprobar manualmente" en Ajustes → Actualizaciones de versión.
- Al pulsar Actualizar ahora en la página Inicio se muestra el progreso.
- O actualiza a mano en el servidor:

```bash
/opt/violet/violet update
```

## Desinstalación

El siguiente comando elimina Violet por completo. **No se puede deshacer.** Borra:

- el programa, los servicios, las actualizaciones automáticas y el servicio de tareas en segundo plano;
- el relé MCP del móvil, la búsqueda web, la cuenta de QQ y la voz;
- la configuración, los datos (historial de chat y memoria) y las copias de seguridad;
- la cuenta de sistema `violet`, el directorio `/opt/violet` y todas las reglas de cortafuegos que abrió Violet.

```bash
curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

También limpia lo que quede tras una instalación a medias, un registro de instalación perdido o una desinstalación anterior que solo quitó el programa. Python y openssl instalados con el gestor de paquetes del sistema se conservan, porque otros programas pueden usarlos.

## Estado

- **Pruebas automáticas**:
  - Todas las funciones tienen pruebas automáticas sin conexión, unas 3000 en total, que se ejecutan con modelos y plataformas de chat simulados y sin red.
  - Cada cambio en el instalador ejecuta la instalación completa, el reinicio automático, la copia de seguridad, la vuelta atrás tras una actualización fallida y la desinstalación en Linux real en GitHub Actions (Ubuntu 22.04 / 24.04, Debian 12, Rocky Linux 9).
- **Uso real**:
  - Los cuatro canales de chat (WeChat, QQ personal, bot oficial de QQ, Telegram) se han probado en las plataformas reales.
  - La importación de historial y memoria desde Project Eve se ha hecho con datos reales.
  - La instalación con un clic de la cuenta personal de QQ también ha funcionado en una red de China continental.
- **Sobre quién es ella**: es un proyecto de compañía. Violet sabe que es un personaje ficticio y nunca finge ser una persona real; las imágenes dibujadas por la IA nunca se hacen pasar por fotos reales.

## Qué hay en el repositorio

| Ruta | Contenido |
|---|---|
| `violet/ai/` | Violet en sí: chat, memoria, personas, estado de ánimo, vida virtual, iniciativa, chat de grupo, herramientas, dibujo, horario |
| `violet/ai/channels/` | Conexiones con WeChat, QQ y Telegram |
| `violet/wx_ilink_py/` | El panel web (Flask) y el cliente iLink de WeChat |
| `violet/persona/` | Plantillas de personas y prompts |
| `violet/tools/` | Instalador, publicación, scripts de instalación de las partes opcionales (búsqueda web, cuenta de QQ, voz, relé del móvil) y herramientas de prueba para desarrollo |
| `violet/tests/` | Pruebas automáticas sin conexión |
| `violet/deploy/` | Scripts de aceptación de la instalación y una forma alternativa de desplegar SearXNG con Docker |

## Proyecto relacionado

[Violet MCP](https://github.com/aileboeth-web/violet-mcp) es una app opcional para Android. Una vez instalada en tu móvil, Violet puede conocer el estado del teléfono dentro de los límites que permitas, como si la pantalla está encendida, la app que estás usando, la batería, los pasos, el calendario y la música que suena. Cada elemento se puede activar o desactivar por separado en el móvil.

## Licencia

MIT, ver [LICENSE](LICENSE).

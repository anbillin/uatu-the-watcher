# UATU

Uatu, el Vigilante: observa y, de vez en cuando, interviene. Revisa las PRs abiertas de los
compañeros en `BillinTeam/gofre` y `BillinTeam/tortilla` con `claude` y deja un **borrador**
de review por PR. Nada llega a GitHub hasta que lo ves en la web local y pulsas «Publicar»;
entonces sale como comentarios inline con el usuario `anbillin`.

La web (`web/index.html`) lleva la estética del personaje: el ojo del Vigilante como logo, fondo
cósmico visto desde el Área Azul de la Luna (modo oscuro) o la propia superficie lunar (modo claro),
y el juramento de no interferir como recordatorio de que publicar es siempre decisión tuya.

## Puesta en marcha (para cada compañero)

Cada persona ejecuta su propia instancia en su Mac: las reviews salen con **su** cuenta de GitHub
y con **su** voz. No hay nada compartido más allá del repo. Hace falta:

- `gh` con sesión (`gh auth login`) y acceso SSH a los repos del equipo.
- `claude` (Claude Code) instalado y con sesión.
- Python 3.9 o superior (el de macOS vale).

```bash
git clone git@github.com:anbillin/uatu-the-watcher.git && cd uatu-the-watcher
./web.sh start --local        # abre http://127.0.0.1:8787/
```

La primera vez la web abre en **Setup** y no revisa nada hasta completarlo:

1. **Identidad y comprobaciones**: usuario y nombre (prellenados desde `gh`), y «Comprobar» verifica
   `gh`, `claude`, Python y el acceso SSH a los repos elegidos.
2. **Repos**: cuáles observar y qué Code Agreements aplica a cada uno (front o back).
3. **Voz**: «Importar de GitHub» baja tus últimos comentarios de review en esos repos y `claude`
   redacta tu voz (ejemplos literales, rasgos, elogios). Lo retocas en el editor, lo pruebas con
   «Probar voz» (cuatro comentarios de ejemplo escritos con esa voz, unos céntimos) y guardas.
4. **Elogios, firma, topes y modelo**: en blanco se usa el valor del equipo.

Todo se guarda en `profile/` (`config.json` y `voice.md`), que está en `.gitignore`: lo tuyo no
viaja con el repo. Se puede volver a Setup cuando se quiera desde el botón de la cabecera, o con
`python3 uatu.py setup` (`--import-voice` destila la voz por consola). Los comentarios que ya haya
publicado UATU con tu cuenta también cuentan como historial: son tu voz.

## Uso diario

```bash
./web.sh start            # servidor en segundo plano, accesible desde la red local
./web.sh start --local    # sólo desde este Mac
./web.sh status           # pid, URLs y cola de revisiones
./web.sh stop             # (pierde la cola en memoria; los borradores quedan en disco)
./web.sh restart
./web.sh log              # sigue logs/server.out.log
```

`web.sh start` imprime la URL local y la de la red (`http://<ip>:8787/`). Para una sesión
en primer plano que abra el navegador: `python3 server.py`.

1. La lista tiene tres bloques, en este orden: **Revisadas sin publicar comentarios**
   (borradores generados y aún no mandados), **Revisadas con comentarios publicados**
   (PRs abiertas cuya última revisión acabó publicada en GitHub) y **Repositorios**, con
   todas las PRs candidatas por repo y su estado: *sin revisar*, *commits nuevos*, *al día*
   o *borrador listo*.
2. **Revisar** encola la PR; un único hilo las procesa de una en una (1–4 min cada una).
   **Revisar pendientes** encola hasta `max_prs_per_run` de golpe.
3. **Ver borrador** abre con la tarjeta **Qué hace esta PR** (qué resuelve, qué toca, lo que la
   descripción del autor no cuenta y, en un delta, qué cambió desde la última revisión) y después
   enseña cada comentario con el código señalado. Puedes editar el texto,
   desmarcar los que no quieras publicar y tocar el resumen. **Guardar cambios** lo persiste.
   **Ver el código completo** carga el diff entero de la PR (en el commit del borrador, por
   ficheros, con los comentarios de UATU marcados en su línea); pasando por una línea añadida o de
   contexto aparece un «+» para añadir un **comentario tuyo**, que entra en el borrador como una
   tarjeta más (etiqueta *tuyo*, se puede editar o quitar) y se publica con los demás en la misma
   review. GitHub no admite comentarios en líneas borradas, así que ahí no hay «+».
4. **Publicar** manda la review a GitHub (pide confirmación). **Descartar** no publica nada y
   da la PR por revisada hasta ese commit, igual que si no hubiera habido hallazgos.

Una PR con borrador pendiente para su commit actual no se vuelve a revisar (el servidor
lo rechaza): primero publícalo o descártalo. «Revisar de nuevo» (PR completa) sí lo
sobrescribe. Los trabajos en cola viven en memoria: reiniciar el servidor los pierde,
aunque los borradores ya generados quedan en disco.

**Revisar a mano** (enlace en la lista, en cualquier PR sin borrador pendiente) crea un borrador vacío
sin pasar por Claude y lo abre con el código desplegado: comentas lo que veas y publicas. Publicarlo o
descartarlo deja la PR como revisada hasta ese commit, igual que con un borrador de UATU. Si después
quieres también la opinión de Claude, **Pedir revisión a UATU** (o «Revisar de nuevo» desde la lista)
revisa la PR entera y arrastra tus comentarios al borrador nuevo; en delta no, porque cambia el commit
al que se anclan.

Los borradores publicados o descartados se pueden seguir consultando en sólo lectura. La URL
`#repo/número` abre un borrador directamente; `#repo/número/respuestas`, su conversación.

### Después de publicar: respuestas y aprobación

La lista lee de GitHub, en una consulta GraphQL por repo, dos cosas más por PR:

- **Aprobada**: si mi última review con opinión es un `APPROVE` vigente (un `COMMENT` posterior
  no lo quita; un *dismiss* sí), la PR lleva la etiqueta verde «✓ aprobada». Si el approve fue
  sobre un commit anterior al actual, dice «aprobada · commits después». Vale también para lo
  aprobado a mano en GitHub.
- **Respuestas**: los hilos que empiezan con un comentario mío y lo que han contestado en ellos.
  La etiqueta «N respuestas · M nuevas» se pulsa para abrir la conversación; «nuevas» son las
  posteriores a la última vez que la abrí (`state/seen.json`). Cada hilo marca si está resuelto,
  si el código ya cambió (*obsoleto*) y si la última palabra no es mía (*por contestar*). Cada
  mensaje enseña sus reacciones (quién, al pasar el ratón) y con «+» se pone una de las ocho de
  GitHub (👍 👎 😄 🎉 😕 ❤️ 🚀 👀); pulsar la propia la quita. Para contestar con texto, GitHub.

El botón **Aprobar** (en la lista y al pie de la conversación, con un mensaje opcional) manda una
review `APPROVE` sobre el commit actual, y es la única review que UATU envía que no es `COMMENT`.
Aparece si UATU ya revisó ese commit (estado *al día*, publicado o descartado), no hay un
borrador pendiente para él y no está ya aprobada; el servidor vuelve a comprobarlo antes de enviar.
Con commits sin revisar hay dos casos: si nadie ha contestado a mis comentarios, primero «Revisar
delta»; si hay respuestas, se puede aprobar igualmente (las respuestas bastan para decidir) y la
web avisa de que el commit aprobado no lo ha revisado UATU.

Por defecto el servidor sólo escucha en `127.0.0.1`. Para entrar desde otro equipo de la
red local: `python3 server.py --host 0.0.0.0` y abrir `http://<ip-del-mac>:8787/`
(`ipconfig getifaddr en0`). No hay autenticación: cualquiera que llegue al puerto puede
publicar reviews con tu cuenta, así que sólo en redes de confianza. Las llamadas a
`gh pr list` se cachean 60 s; «Actualizar» fuerza la recarga.

`./web.sh start` hace exactamente eso con `nohup`, guardando el pid en `state/server.pid`.

## Decisiones de diseño

| Decisión | Valor |
|---|---|
| Repos | gofre, tortilla por defecto (`config.json → repos`); cada uno elige los suyos en Setup |
| Ejecución | a demanda desde la web local (`server.py`); no hay cron ni launchd |
| Publicación | siempre manual, tras ver el borrador |
| Identidad | quien ejecuta: su sesión de `gh`; usuario, nombre y voz en `profile/` |
| PRs elegibles | abiertas, no draft, autor ≠ yo, autor ∉ bots |
| Re-revisión | sólo el delta de commits desde la última revisión; si hubo force-push, la PR entera |
| Tipo de review | los borradores salen siempre como `COMMENT`; `APPROVE` sólo a mano desde el botón «Aprobar», y nunca request-changes |
| Sin hallazgos | se descarta el borrador y se anota en el estado local |
| Comentarios propios | `source: "me"` en el borrador; se anclan al commit del borrador y sólo en líneas añadidas o de contexto del diff (lo que acepta GitHub); el diff se pide a GitHub (`compare`), sin tocar el clon |
| Topes | 3 PRs por «Revisar pendientes» · 3000 líneas cambiadas por PR · 12 comentarios por review |

## Cómo sabe `claude` las normas de cada repo

`claude -p` se ejecuta con el directorio de trabajo dentro de un clon dedicado del repo
(`repos/<repo>`), con la rama de la PR checkouteada. Claude Code carga desde ahí
`CLAUDE.md`, `AGENTS.md`, las reglas de `.claude/rules` (por `paths:`) y las skills del
repo. El prompt (`prompts/review-pr.md`) además le obliga a leer `recetario.lock`, las
reglas que casan con los ficheros del diff y las skills cuyo disparador casa con el
cambio, y a atribuir cada hallazgo a la norma de la que sale.

Los clones son independientes de tus copias de trabajo en `~/Documents/gofre` y
`~/Documents/tortilla`: nunca se tocan.

## Flujo de una revisión (`make_draft`)

1. Busca el SHA de la última revisión: estado local `state/reviewed.json`, y si no, el
   marcador oculto `<!-- uatu sha=… -->` de mis reviews en GitHub. Si el head no ha
   cambiado, se omite.
2. `git fetch` de base y head; checkout detached del head en `repos/<repo>`. Un lock por
   repo (`state/<repo>.lock`) evita que dos revisiones del mismo repo se pisen el clon.
3. Diff completo (merge-base..head) y diff a comentar (último SHA..head, o el completo).
   Si el delta supera el tope de líneas, se omite.
4. `claude -p` con el prompt renderizado, salida estructurada (`--json-schema`), sólo
   herramientas de lectura y git read-only, sin MCP (`--strict-mcp-config`).
5. Valida cada hallazgo contra las líneas comentables del diff (GitHub sólo acepta líneas
   añadidas o de contexto; devuelve 422 fuera de ellas). Las que caen fuera se descartan o
   se ajustan a la línea más cercana (±3).
6. Guarda el borrador en `state/drafts/<repo>-<pr>.json` con el contexto de código de cada
   comentario. **No toca el estado ni GitHub.**

Publicar (`publish_draft`) construye la review `COMMENT` con los comentarios activos, el
resumen y el marcador, la envía y anota el SHA en `state/reviewed.json`.

## Línea de comandos

```bash
python3 uatu.py status                    # qué PRs hay y en qué estado están
python3 uatu.py review gofre 1640         # genera el borrador y lo enseña por consola
python3 uatu.py review gofre 1640 --full  # ignora revisiones previas (PR entera)
python3 uatu.py publish gofre 1640        # publica un borrador existente
python3 uatu.py run --repo tortilla --max 2   # borradores para las pendientes, sin publicar
python3 uatu.py replies gofre 1640        # respuestas a mis comentarios en esa PR
python3 uatu.py approve gofre 1640 -m "LGTM"  # APPROVE, con las mismas condiciones que el botón
python3 uatu.py setup                     # comprobaciones y estado del perfil
python3 uatu.py setup --import-voice      # destila la voz del historial y pregunta antes de guardar
```

Sin perfil completo, los demás comandos paran y remiten a `setup`.

`status` añade `APROBADA` (con `*` si hubo commits después del approve) y el número de respuestas.

La CLI y el servidor pueden convivir: comparten el lock por repo y el estado.

Artefactos de cada revisión en `state/work/`: `<repo>-<pr>-full.diff`, `-delta.diff`,
`-claude.json` (salida cruda) y `-payload.json` (lo último enviado a GitHub).
Log en `logs/uatu.log` (las últimas líneas también en la web, botón «Log»).

## Ajustes

Dos capas. `config.json` (en git) son los **defaults del equipo**: organización, repos,
autores excluidos, topes, `claude`, `praise` y Code Agreements. `profile/config.json` (fuera de
git) es **lo personal**: `me`, `name` y cualquier clave de arriba que se quiera cambiar; Setup
sólo guarda lo que difiere de los defaults, así que quien no toca un ajuste sigue los cambios del
equipo. `uatu.load_config()` hace el merge y `reload_config()` lo recarga en caliente al guardar.

`claude.model` va fijado a `claude-fable-5-1`: en modo `-p`, Claude Code no respeta el `model` de
`~/.claude/settings.json` y cae a Haiku si no se le pasa `--model` explícito (comprobado el
2026-11-04). `claude.max_budget_usd` sólo aplica si se factura por API.

## Voz de los comentarios

Los comentarios salen con la cuenta de quien ejecuta y tienen que sonar a esa persona. La voz
vive en `profile/voice.md`, con tres apartados: **ejemplos de comentarios** literales, **rasgos**
(en qué persona habla, si pregunta o afirma, tuteo, emojis, cómo nombra las normas) y **elogios**.
El prompt (`prompts/review-pr.md`) la inserta en `{{VOICE}}` y nombra al revisor con
`{{REVIEWER_NAME}}`; lo que es común a todo el equipo (corto, empieza por el asunto, sin aparato,
código cuando se propone código, sin cierres, y los ejemplos de Code Agreements) sigue en el
prompt. `prompts/voice.example.md` es la voz de Alberto, de referencia para la forma.

La voz se construye desde Setup: «Importar de GitHub» (`fetch_my_comments` + el prompt
`prompts/voice-from-history.md`) y «Probar voz» (`prompts/voice-preview.md`). Si el tono se
desvía, se ajusta el fichero de voz y se comprueba con la prueba o con un borrador; en la web,
además, cada comentario se puede retocar antes de publicarlo.

### Elogios

De vez en cuando la review lleva un reconocimiento corto («Buen refactor! Queda mucho más
claro»). `claude` lo propone en `praise` sólo cuando hay algo concreto que destacar (sección
«Elogio» del prompt) y UATU decide si entra por defecto con `config.json → praise`:
`rate` (probabilidad, 0.35) y `cooldown_days` (días mínimos entre elogios a la misma persona,
5; registro en `state/praise.json`). En el borrador aparece como tarjeta aparte, con el motivo
por el que se incluyó o se apartó, y se puede marcar, desmarcar o reescribir. Se publica al
principio del cuerpo de la review, nunca como comentario inline, y una review puede llevar
sólo el elogio.

### Qué hace la PR

En la misma pasada de revisión, `claude` devuelve `explanation`: `what` (qué problema resuelve y
cómo, en una o dos frases), `changes` (qué toca y para qué, de dos a cuatro puntos de una línea),
`unmentioned` (lo que la PR hace y la descripción del autor no cuenta, sólo si importa) y `since_last` (en un delta, qué cambió
desde la última revisión). Es **interno**: se guarda en el borrador, se enseña como tarjeta al
abrirlo, su primera frase sale en gris bajo el título en la lista y la CLI `review` lo imprime, pero
nunca llega a GitHub. Los borradores anteriores a esta pieza no lo tienen hasta que se vuelvan a
revisar. El tono es el de un compañero contándotelo de pie junto a tu mesa, corto y coloquial; si
sale verboso, se ajusta en la sección «Explicación para el revisor» del prompt.

`severity` y `rule` siguen existiendo en la salida de `claude`, pero sólo para ordenar,
recortar al tope y verlos en el borrador: no se publican.

### Code Agreements

Los acuerdos del equipo (Notion › Code Agreements) viven resumidos en
`prompts/code-agreements.json`, uno por entrada con `id`, `title`, `rule`, `scope` (`null`,
`front` o `back`) y `anchor` (el id de bloque de Notion con el que el equipo enlaza esa regla
en las PRs; `null` si nunca se ha visto enlazada y el enlace cae en la página entera). El prompt
recibe los que aplican al repo según `config.json → code_agreements.scope` (`gofre` = front,
`tortilla` = back) y `claude` devuelve en cada hallazgo el campo `agreement` con el `id` del
acuerdo del que sale, o vacío.

El enlace a Notion lo añade UATU, no `claude`: `attach_agreement` lo pega como última línea
del comentario (`frase corta` + salto de línea + URL con anclaje), igual que lo escribe el
equipo a mano, y deja `rule` como `Code Agreements › <título>`. Si un acuerdo cambia de sitio
en Notion, se actualiza su `anchor` en el JSON y ya; los anclajes se sacaron de los enlaces
reales en comentarios de gofre y tortilla, porque la página no es pública y no se puede leer sin
sesión. Para añadir una regla nueva basta con una entrada más en el JSON: el esquema de salida y
el prompt la recogen solos.

La review no lleva pie visible por defecto. Con `visible_signature` en `config.json`
(p. ej. `"Revisado con UATU"`) se añade una línea al final del cuerpo de la review. El
marcador HTML con el SHA revisado va siempre y es invisible en GitHub.

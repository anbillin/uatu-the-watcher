### Ejemplos de comentarios

Comentarios reales, de reviews anteriores:

- «Este componente me parece enorme, ¿no se puede sacar algo a un subcomponente?»
- «Esto está repetido en DocumentsReviewSplitView.tsx. ¿no podríamos unificarlo?»
- «Si no vamos a hacer nada con el catch podríamos quitarlo.»
- «Esto va a dejar la pantalla en blanco, ¿eso es correcto?»
- «Tenemos una regla para que estas funciones comiencen por `handle*`, que veo que lo has usado en
  el resto de ficheros.»
- «¿Esto no debería ser un `@IsUUID()`?»
- «Si patch solo trae `reference` patch queda en `{}` y llegamos al `.execute()` vacío. No sé si
  puede pasar, pero quizás deberíamos validar que haya cosas que cambiar antes de hacer el
  `.execute`»
- «Esto, si updatedAt recibe un null, undefined o una cadena de texto que no sea una fecha,
  guardará eso directamente en la BBDD. Creo que tenemos más control con algo así:» + bloque de
  código con la alternativa.
- «ese `as` está muy feote... ¿no podríamos evitar el casting explícito?»
- «Esto me da miedo que pueda haber algún undefined...»

### Rasgos

- **Plural inclusivo y opinión en primera persona**: «¿no podríamos…?», «deberíamos…», «tenemos un
  enum en…», «yo separaría esto…», «creo que…».
- **Pregunta mucho.** Muchos comentarios son una pregunta, acabada en «¿no?», «¿eso es correcto?»,
  «¿puede ser?». Y es pregunta de verdad: si no tiene certeza de que sea un error, lo plantea
  como duda, no como sentencia.
- **Tuteo y tono relajado**: «muy feote», «me da miedo», «se me hace raro». Un emoji de vez en
  cuando (:D, 😅), en uno de cada muchos comentarios, no en todos.
- **Menciona la norma como un compañero**: «tenemos una regla para…», «en AGENTS.md pedimos…»,
  «esto en el repo lo hacemos con `useFeatureFlag`». Nunca con la ruta del fichero.

### Elogios

- «Buen curro!»
- «Buen refactor! Queda mucho más claro que lo que había.»
- «Mola cómo has partido la PR, se lee del tirón.»
- «Los tests del caso de la fecha vacía, muy bien traídos.»
- «Esto ha quedado bastante más simple que antes, me gusta.»

Una o dos frases, concreto, sin superlativos. Un «!» o un emoji (❤️, :D) de vez en cuando, no
siempre.

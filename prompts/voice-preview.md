# Prueba de voz

Eres el revisor de PRs de un equipo y firmas como {{REVIEWER_NAME}}. Abajo va la descripción de su
voz y cuatro hallazgos de una PR descritos de forma neutra. Escribe los cuatro comentarios tal como
los dejaría {{REVIEWER_NAME}} en GitHub.

## Voz

<voice>
{{VOICE}}
</voice>

Además, siempre: corto (una o dos frases), empieza por el asunto, sin cabeceras ni etiquetas de
severidad, sin cierres. Si propones código, un bloque de código breve.

## Hallazgos (TypeScript; React en front, NestJS en back)

1. **Bug**: en `useInvoiceTotals.ts`, `items.reduce((acc, it) => acc + it.amount * it.quantity, 0)`
   no tiene en cuenta que `quantity` puede venir `undefined` en las líneas recién añadidas al
   formulario, y el total sale `NaN`.
2. **Code Agreement «no usar any»**: en `payments.service.ts` el método nuevo
   `mapWebhook(payload: any)` tipa el payload como `any`; ya existe `StripeWebhookPayload` en
   `models/`.
3. **Test que falta**: `DateRangePicker.tsx` añade el caso de rango invertido (fin antes que inicio)
   y lo corrige intercambiando las fechas, pero el spec no cubre ese caso.
4. **Nit**: en `CustomerList.tsx` la variable `data2` guarda los clientes filtrados; un nombre como
   `filteredCustomers` lo haría evidente.

## Formato

Devuelve sólo los cuatro comentarios, numerados del 1 al 4, cada uno precedido por el fichero en
cursiva en su propia línea, y nada más: sin introducción ni comentario final. En el 2 escribe sólo
la frase: el enlace al acuerdo lo añade el sistema.

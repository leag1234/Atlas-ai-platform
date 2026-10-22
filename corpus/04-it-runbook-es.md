---
doc_id: runbook-incidente-db
domaine: technique-it
langue: es
titre: Runbook — Incidente de base de datos saturada
version: 3.1
date_maj: 2026-02-20
---

# Runbook — Saturación de la base de datos de producción

## Objetivo

Este procedimiento describe los pasos para diagnosticar y resolver una saturación de
conexiones en la base de datos PostgreSQL de producción. Tiempo objetivo de resolución
(RTO): **treinta minutos**.

## Síntomas

La alerta se dispara cuando el número de conexiones activas supera el 90 por ciento del
límite configurado (200 conexiones). Los usuarios reportan lentitud o errores de tipo
"too many connections". El panel de monitorización muestra la latencia de consultas por
encima de 500 milisegundos.

## Paso 1 — Confirmar el incidente

Conéctese al servidor de monitorización y verifique la métrica `pg_stat_activity`. Si el
número de conexiones activas es inferior a 150, no se trata de una saturación real y debe
buscarse otra causa. No reinicie la base de datos en esta fase.

## Paso 2 — Identificar las consultas bloqueantes

Ejecute la consulta de diagnóstico para listar las sesiones con estado "idle in transaction"
desde hace más de cinco minutos. Estas sesiones suelen ser la causa principal. Anote los
identificadores de proceso (PID) correspondientes.

## Paso 3 — Terminar las sesiones problemáticas

Termine únicamente las sesiones "idle in transaction" identificadas en el paso anterior,
usando la función `pg_terminate_backend`. **Nunca** termine sesiones activas que estén
ejecutando una consulta legítima, ya que esto podría corromper una transacción en curso.

## Paso 4 — Verificar la recuperación

Espere dos minutos y vuelva a comprobar el número de conexiones. Si desciende por debajo
de 150, el incidente está resuelto. Si persiste, escale al equipo de guardia de nivel 2 e
inicie el procedimiento de reinicio controlado descrito en el runbook RB-DB-07.

## Paso 5 — Post-incidente

Redacte un informe en un plazo de cuarenta y ocho horas. Debe incluir la causa raíz, la
cronología y las acciones correctivas. Si la causa es una fuga de conexiones en la
aplicación, cree un ticket de prioridad alta para el equipo de desarrollo.

-- Una fila por social_id x journey x campaña con la CANTIDAD de impresiones (push_delivered) y clics (push_session).
-- CTR = clics / impresiones x 100 (se calcula en metrics/calculate_index.py).
-- Todos los journeys de tests.csv en una sola pasada sobre la tabla de eventos.
-- Sin límite superior de fecha: un usuario puede abrir el push días o semanas después.
-- El join es contra los social_id DISTINTOS de la tmp: la tmp tiene una fila por producto
-- y un join directo duplicaría impresiones/clics de quienes compraron vuelo + hotel.
WITH push AS (
    SELECT
        event.social_id                                                       AS social_id,
        element_at(event.event_data, 'journey_name')                          AS journey_name,
        element_at(event.event_data, 'campaign_name')                         AS variante_raw,
        SUM(CASE WHEN event.event_type = 'push_delivered' THEN 1 ELSE 0 END) AS delivered,
        SUM(CASE WHEN event.event_type = 'push_session'   THEN 1 ELSE 0 END) AS clicked
    FROM sot.insider_mobile_app_events
    WHERE element_at(event.event_data, 'journey_name') IN (__JOURNEY_NAMES__)
      AND event.event_type IN ('push_delivered', 'push_session')
      AND datetime >= '__FECHA_MIN__'
      AND event.social_id IS NOT NULL
    GROUP BY 1, 2, 3
),
audiencia AS (
    SELECT DISTINCT social_id
    FROM tmp.socials_variables_2nd_trip
)
SELECT
    p.social_id,
    p.journey_name,
    p.variante_raw,
    p.delivered,
    p.clicked
FROM push p
INNER JOIN audiencia v ON p.social_id = v.social_id

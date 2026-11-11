-- UNA fila por social_id con sus atributos de perfil.
-- Se agrupa por social (no DISTINCT): traveler_type depende de adults/children de cada producto,
-- y con DISTINCT un social con vuelo (2 adultos) + hotel (2 adultos + 1 niño) quedaría duplicado.
-- Se toma el máximo de adultos/niños entre sus productos.
WITH push_socials AS (
    -- Solo los socials que recibieron/abrieron un push de los journeys configurados:
    -- la tmp tiene ~5.7M socials y bajarla entera supera el límite de 10 min de Trino.
    SELECT DISTINCT event.social_id AS social_id
    FROM sot.insider_mobile_app_events
    WHERE element_at(event.event_data, 'journey_name') IN (__JOURNEY_NAMES__)
      AND event.event_type IN ('push_delivered', 'push_session')
      AND datetime >= '__FECHA_MIN__'
      AND event.social_id IS NOT NULL
)
SELECT
    v.social_id,
    MAX(v.country)         AS country,
    MAX(v.brand)           AS brand,
    MAX(v.status)          AS status,
    MAX(v.card)            AS card,
    MAX(v.used_cupon)      AS used_cupon,
    MAX(v.cantidad_cuotas) AS cantidad_cuotas,
    CASE
        WHEN MAX(v.children) > 0 THEN 'Familia'
        WHEN MAX(v.adults) >= 3  THEN 'Grupo'
        WHEN MAX(v.adults) = 2   THEN 'Pareja'
        WHEN MAX(v.adults) = 1   THEN 'Solo'
        ELSE                          'Sin datos'
    END AS traveler_type
FROM tmp.socials_variables_2nd_trip v
INNER JOIN push_socials p ON v.social_id = p.social_id
GROUP BY v.social_id

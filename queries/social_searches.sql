-- Resumen de búsquedas por social, una fila por social.
-- La tabla del lake se llama tmp.socials_trafico_2nd_trip (tmp.socials_searches_2nd_trip no existe).
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
    s.social_id,
    COUNT(DISTINCT s.searchid)         AS nro_busquedas,
    COUNT(DISTINCT s.destino_buscado)  AS destinos_distintos,
    COUNT(DISTINCT s.producto_buscado) AS productos_distintos
FROM tmp.socials_trafico_2nd_trip s
INNER JOIN push_socials p ON s.social_id = p.social_id
GROUP BY s.social_id

-- Impresiones y clics de PUSH y EMAIL de los journeys de tests.csv, cruzados con la audiencia y
-- AGREGADOS en Trino: email llega a millones de socials y bajarlo por social no escala.
--
--   Push:  impresión = push_delivered   · clic = push_session      (sot.insider_mobile_app_events)
--   Email: impresión = email_delivered  · clic = email_click       (sot.insider_email_events)
--          la campaña de email está en event_data['personalization_name'] (no hay campaign_name).
--
-- Clics = SOCIALS que clickearon (máx. 1 por social x campaña), en los dos canales: un email puede
-- tener muchos clics del mismo social (varios links, filtros antispam que abren todo), y así push
-- y email se miden igual. CTR = clics / impresiones x 100 (se calcula en el dashboard).
--
-- Salida en formato largo: una fila por journey x campaña x canal x país x DIMENSIÓN x segmento.
-- Cada dimensión se agrega por separado (GROUPING SETS, una sola pasada); la dimensión 'country'
-- es el total por país y es la que se usa para totales. Las claves de dimensión y los rangos
-- coinciden con DIMENSIONES de metrics/calculate_index.py.
-- Sin límite superior de fecha: un usuario puede abrir el push/email días después.
WITH push AS (
    SELECT
        event.social_id                                                   AS social_id,
        element_at(event.event_data, 'journey_name')                      AS journey_name,
        element_at(event.event_data, 'campaign_name')                     AS variante_raw,
        'Push'                                                            AS canal,
        count_if(event.event_type = 'push_delivered')                     AS delivered,
        LEAST(count_if(event.event_type = 'push_session'), 1)             AS clicked
    FROM sot.insider_mobile_app_events
    WHERE element_at(event.event_data, 'journey_name') IN (__PUSH_JOURNEYS__)
      AND event.event_type IN ('push_delivered', 'push_session')
      AND datetime >= '__FECHA_MIN__'
      AND event.social_id IS NOT NULL
    GROUP BY 1, 2, 3
),
email AS (
    SELECT
        event.social_id                                                   AS social_id,
        element_at(event.event_data, 'journey_name')                      AS journey_name,
        element_at(event.event_data, 'personalization_name')              AS variante_raw,
        'Email'                                                           AS canal,
        count_if(event.event_type = 'email_delivered')                    AS delivered,
        LEAST(count_if(event.event_type = 'email_click'), 1)              AS clicked
    FROM sot.insider_email_events
    WHERE element_at(event.event_data, 'journey_name') IN (__EMAIL_JOURNEYS__)
      AND event.event_type IN ('email_delivered', 'email_click')
      AND datetime >= '__FECHA_MIN__'
      AND event.social_id IS NOT NULL
    GROUP BY 1, 2, 3
),
ev AS (
    SELECT * FROM push
    UNION ALL
    SELECT * FROM email
),
ev_socials AS (
    SELECT DISTINCT social_id FROM ev
),
-- UNA fila por social: la tmp tiene una fila por producto (vuelo + hotel duplicaría).
-- traveler_type usa el máximo de adultos/niños entre sus productos.
attrs AS (
    SELECT
        v.social_id,
        MAX(v.country)                        AS country,
        MAX(v.status)                         AS status,
        MAX(v.card)                           AS card,
        MAX(v.used_cupon)                     AS used_cupon,
        MAX(v.cantidad_cuotas)                AS cantidad_cuotas,
        CASE
            WHEN MAX(v.children) > 0 THEN 'Familia'
            WHEN MAX(v.adults) >= 3  THEN 'Grupo'
            WHEN MAX(v.adults) = 2   THEN 'Pareja'
            WHEN MAX(v.adults) = 1   THEN 'Solo'
            ELSE                          'Sin datos'
        END                                   AS traveler_type
    FROM tmp.socials_variables_2nd_trip v
    INNER JOIN ev_socials e ON v.social_id = e.social_id
    GROUP BY v.social_id
),
-- La tabla del lake se llama tmp.socials_trafico_2nd_trip (tmp.socials_searches_2nd_trip no existe).
searches AS (
    SELECT
        s.social_id,
        COUNT(DISTINCT s.searchid)          AS nro_busquedas,
        COUNT(DISTINCT s.destino_buscado)   AS destinos_distintos,
        COUNT(DISTINCT s.producto_buscado)  AS productos_distintos
    FROM tmp.socials_trafico_2nd_trip s
    INNER JOIN ev_socials e ON s.social_id = e.social_id
    GROUP BY s.social_id
),
-- Un registro por social x campaña con sus segmentos ya armados (nulos/vacíos -> 'Sin datos').
perfil AS (
    SELECT
        ev.journey_name, ev.variante_raw, ev.canal, ev.delivered, ev.clicked,
        COALESCE(NULLIF(TRIM(a.country), ''), 'Sin datos')                      AS pais,
        COALESCE(a.traveler_type, 'Sin datos')                                  AS traveler_type,
        COALESCE(NULLIF(TRIM(a.card), ''), 'Sin datos')                         AS card,
        COALESCE(NULLIF(TRIM(a.status), ''), 'Sin datos')                       AS status,
        CASE a.used_cupon WHEN 1 THEN 'Sí' WHEN 0 THEN 'No' ELSE 'Sin datos' END AS used_cupon,
        -- 1 cuota = No; más de 1 = Sí; 0 o nulo = Sin datos (no se sabe si fue un pago)
        CASE WHEN a.cantidad_cuotas > 1 THEN 'Sí'
             WHEN a.cantidad_cuotas = 1 THEN 'No' ELSE 'Sin datos' END          AS uso_cuotas,
        -- sin búsquedas -> rango "0"
        CASE WHEN COALESCE(s.nro_busquedas, 0) = 0  THEN '0'
             WHEN s.nro_busquedas = 1               THEN '1'
             WHEN s.nro_busquedas <= 5              THEN '2-5'
             WHEN s.nro_busquedas <= 10             THEN '6-10'
             ELSE '10+' END                                                     AS rango_busquedas,
        CASE WHEN COALESCE(s.destinos_distintos, 0) >= 5 THEN '5+'
             ELSE CAST(COALESCE(s.destinos_distintos, 0) AS varchar) END        AS destinos_distintos,
        CASE WHEN COALESCE(s.productos_distintos, 0) >= 3 THEN '3+'
             ELSE CAST(COALESCE(s.productos_distintos, 0) AS varchar) END       AS productos_distintos
    FROM ev
    INNER JOIN attrs a ON ev.social_id = a.social_id      -- solo socials de la audiencia 2nd trip
    LEFT JOIN searches s ON ev.social_id = s.social_id
)
SELECT
    journey_name,
    variante_raw,
    canal,
    pais,
    CASE WHEN grouping(traveler_type) = 0       THEN 'traveler_type'
         WHEN grouping(card) = 0                THEN 'card'
         WHEN grouping(status) = 0              THEN 'status'
         WHEN grouping(used_cupon) = 0          THEN 'used_cupon'
         WHEN grouping(uso_cuotas) = 0          THEN 'uso_cuotas'
         WHEN grouping(rango_busquedas) = 0     THEN 'rango_busquedas'
         WHEN grouping(destinos_distintos) = 0  THEN 'destinos_distintos'
         WHEN grouping(productos_distintos) = 0 THEN 'productos_distintos'
         ELSE 'country' END                                                     AS dimension,
    COALESCE(traveler_type, card, status, used_cupon, uso_cuotas, rango_busquedas,
             destinos_distintos, productos_distintos, pais)                     AS segmento,
    SUM(delivered)                                                              AS delivered,
    SUM(clicked)                                                                AS clicked,
    COUNT(*)                                                                    AS socials,
    count_if(clicked > 0 AND delivered = 0)                                     AS clic_sin_imp
FROM perfil
GROUP BY GROUPING SETS (
    (journey_name, variante_raw, canal, pais),
    (journey_name, variante_raw, canal, pais, traveler_type),
    (journey_name, variante_raw, canal, pais, card),
    (journey_name, variante_raw, canal, pais, status),
    (journey_name, variante_raw, canal, pais, used_cupon),
    (journey_name, variante_raw, canal, pais, uso_cuotas),
    (journey_name, variante_raw, canal, pais, rango_busquedas),
    (journey_name, variante_raw, canal, pais, destinos_distintos),
    (journey_name, variante_raw, canal, pais, productos_distintos)
)

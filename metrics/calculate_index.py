"""Cálculo de CTR por campaña y segmento, y construcción de la matriz para el dashboard.

    CTR = (Clics / Impresiones) x 100
        Impresiones = eventos push_delivered / email_delivered
        Clics       = socials que clickearon (push_session / email_click), máx. 1 por social x campaña

    Diferencia vs. promedio = CTR_campaña / CTR_promedio_segmento - 1
        CTR_promedio_segmento = clics de todas las campañas del test / impresiones de todas las campañas del test

Un "test" agrupa los journeys que solo difieren en el país (ej. PRECIO_FLEXIBILIDAD_{AR,MX,..}_W44).
La matriz se guarda desagregada por test x país del social, para que el dashboard pueda filtrar
por país y recalcular el CTR sobre la selección.
"""
from datetime import datetime

import pandas as pd

SIN_DATOS = 'Sin datos'

# Secciones de la matriz, en el orden en que se muestran en el dashboard.
#   key:    nombre de la dimensión; debe coincidir con la columna 'dimension' de queries/matrix_events.sql
#   source: de qué tabla del lake sale (attrs / searches), solo informativo
#   column: columna del social (para convertir caches viejos con a_formato_largo)
#   order:  orden fijo de segmentos (None = por impresiones desc)
# Para agregar una dimensión: sumarla acá Y en el SELECT / GROUPING SETS de matrix_events.sql.
DIMENSIONES = [
    {'key': 'traveler_type', 'label': 'Tipo de viajero', 'icon': '🧳', 'source': 'attrs',
     'column': 'traveler_type', 'order': ['Solo', 'Pareja', 'Familia', 'Grupo', SIN_DATOS]},
    {'key': 'card', 'label': 'Tarjeta', 'icon': '💳', 'source': 'attrs',
     'column': 'card', 'order': None},
    {'key': 'status', 'label': 'Segmento', 'icon': '📊', 'source': 'attrs',
     'column': 'status', 'order': ['Nuevo', 'Activo', 'Dormido', SIN_DATOS]},
    {'key': 'country', 'label': 'País', 'icon': '🌍', 'source': 'attrs',
     'column': 'country', 'order': None},
    {'key': 'used_cupon', 'label': 'Usó cupón', 'icon': '🎟️', 'source': 'attrs',
     'column': 'used_cupon', 'order': ['Sí', 'No', SIN_DATOS]},
    {'key': 'uso_cuotas', 'label': 'Pagó en cuotas', 'icon': '🧾', 'source': 'attrs',
     'column': 'uso_cuotas', 'order': ['Sí', 'No', SIN_DATOS]},
    {'key': 'rango_busquedas', 'label': 'Búsquedas previas', 'icon': '🔍', 'source': 'searches',
     'column': 'rango_busquedas', 'order': ['0', '1', '2-5', '6-10', '10+']},
    {'key': 'destinos_distintos', 'label': 'Destinos distintos buscados', 'icon': '🗺️', 'source': 'searches',
     'column': 'destinos_distintos', 'order': ['0', '1', '2', '3', '4', '5+']},
    {'key': 'productos_distintos', 'label': 'Productos distintos buscados', 'icon': '🧩', 'source': 'searches',
     'column': 'productos_distintos', 'order': ['0', '1', '2', '3+']},
]


# ---------------------------------------------------------------------------
# Cálculo de CTR
# ---------------------------------------------------------------------------
def _agrupar(df, dimension):
    """Suma impresiones/clics por segmento x campaña y calcula CTR (%), CTR promedio (%) y diferencia."""
    g = (df.groupby([dimension, 'variante'], dropna=False)[['delivered', 'clicked']]
           .sum().reset_index())
    tot = (g.groupby(dimension, dropna=False)[['delivered', 'clicked']].sum()
             .rename(columns={'delivered': 'delivered_total', 'clicked': 'clicked_total'})
             .reset_index())
    g = g.merge(tot, on=dimension, how='left')
    g['ctr_pct'] = 100 * g['clicked'] / g['delivered'].where(g['delivered'] > 0)
    g['ctr_promedio_pct'] = 100 * g['clicked_total'] / g['delivered_total'].where(g['delivered_total'] > 0)
    ratio = g['ctr_pct'] / g['ctr_promedio_pct'].where(g['ctr_promedio_pct'] > 0)
    g['index_pct'] = ((ratio - 1) * 100).round()
    return g


def calcular_ctr(df, dimension, min_delivered=0):
    """Matriz pivotada: filas = valores de `dimension`, columnas = campañas, valores = CTR % (2 decimales).

    CTR = (Clics / Impresiones) x 100. df debe tener [variante, delivered, clicked, dimension]
    y corresponder a UN test (las campañas de tests distintos no se comparan entre sí).
    """
    g = _agrupar(df, dimension)
    g = g[g['delivered_total'] >= min_delivered]
    return g.pivot(index=dimension, columns='variante', values='ctr_pct').round(2)


def calcular_indice(df, dimension, min_delivered=0):
    """Matriz pivotada con la diferencia % (int) del CTR de cada campaña vs. el promedio del segmento."""
    g = _agrupar(df, dimension)
    g = g[g['delivered_total'] >= min_delivered]
    return g.pivot(index=dimension, columns='variante', values='index_pct').astype('Int64')


# ---------------------------------------------------------------------------
# Preparación de dimensiones
# ---------------------------------------------------------------------------
def _cap(n, tope):
    n = int(n)
    return f'{tope}+' if n >= tope else str(n)


def _rango_busquedas(n):
    n = int(n)
    if n <= 0:
        return '0'
    if n == 1:
        return '1'
    if n <= 5:
        return '2-5'
    if n <= 10:
        return '6-10'
    return '10+'


def _uso_cuotas(v):
    """1 cuota = No; más de 1 = Sí; 0 o nulo = Sin datos (no se sabe si fue un pago)."""
    if pd.isna(v) or v <= 0:
        return SIN_DATOS
    return 'Sí' if v > 1 else 'No'


def _texto(serie):
    """Normaliza a string; nulos y vacíos -> 'Sin datos'."""
    s = serie.astype('object').where(serie.notna(), None)
    return s.map(lambda v: SIN_DATOS if v is None or str(v).strip() == '' else str(v).strip())


LARGO_KEYS = ['journey_name', 'variante_raw', 'canal', 'pais']
LARGO_VALS = ['delivered', 'clicked', 'socials', 'clic_sin_imp']


def a_formato_largo(wide_df):
    """Pasa una tabla con valores CRUDOS del social (una fila por social o perfil) al formato largo que
    devuelve queries/matrix_events.sql: journey x campaña x canal x país x dimensión x segmento.
    Solo se usa para convertir caches viejos; con datos nuevos los segmentos se arman en SQL."""
    e = _preparar(wide_df)
    partes = []
    for dim in DIMENSIONES:
        col = dim['column']
        keys = LARGO_KEYS if col == 'country' else LARGO_KEYS + [col]
        g = e.groupby(keys, dropna=False)[LARGO_VALS].sum().reset_index()
        g['dimension'] = dim['key']
        g['segmento'] = g['pais'] if col == 'country' else g[col]
        partes.append(g[LARGO_KEYS + ['dimension', 'segmento'] + LARGO_VALS])
    return pd.concat(partes, ignore_index=True)


def _preparar(events_df):
    """Arma las columnas de segmento a partir de los valores crudos del social (mismos cortes que
    queries/matrix_events.sql). Lleva `pais` (país del social) para el filtro."""
    e = events_df.copy()
    for col in ('delivered', 'clicked'):
        e[col] = pd.to_numeric(e[col], errors='coerce').fillna(0).astype(int)
    for col in ('traveler_type', 'card', 'status', 'country'):
        e[col] = _texto(e[col])
    e['pais'] = e['country']
    e['canal'] = _texto(e['canal'])
    cupon = pd.to_numeric(e['used_cupon'], errors='coerce')
    e['used_cupon'] = cupon.map({1: 'Sí', 0: 'No'}).fillna(SIN_DATOS)
    e['uso_cuotas'] = pd.to_numeric(e['cantidad_cuotas'], errors='coerce').map(_uso_cuotas)
    # Búsquedas: los socials sin búsquedas vienen en 0 y van al rango "0".
    for col in ('nro_busquedas', 'destinos_distintos', 'productos_distintos'):
        e[col] = pd.to_numeric(e[col], errors='coerce').fillna(0)
    e['rango_busquedas'] = e['nro_busquedas'].map(_rango_busquedas)
    e['destinos_distintos'] = e['destinos_distintos'].map(lambda n: _cap(n, 5))
    e['productos_distintos'] = e['productos_distintos'].map(lambda n: _cap(n, 3))
    return e


def _ordenar(segmentos, totales, order):
    """Ordena segmentos según el orden fijo o por impresiones desc ("Sin datos" al final)."""
    if isinstance(order, list):
        pos = {v: i for i, v in enumerate(order)}
        return sorted(segmentos, key=lambda v: (pos.get(v, len(order)), -totales[v], v))
    return sorted(segmentos, key=lambda v: (v == SIN_DATOS, -totales[v], v))


def _estructura_tests(push_df, tests_config):
    """Tests (en el orden de tests.csv) con sus campañas. Se agregan al final las campañas que
    aparecen en Insider pero no están en el CSV; se omiten los tests sin ningún evento."""
    tests = []
    con_datos = push_df.groupby('journey_label')['variante'].agg(lambda s: list(dict.fromkeys(s.dropna())))
    for t in tests_config or []:
        if t['label'] not in con_datos.index:
            continue
        campaigns = [{'label': c['label'], 'mensaje': c.get('mensaje', ''), 'canales': c.get('canales', [])}
                     for c in t['campaigns']]
        conocidas = {c['label'] for c in campaigns}
        campaigns += [{'label': v, 'mensaje': '', 'canales': []} for v in sorted(con_datos[t['label']]) if v not in conocidas]
        tests.append({'label': t['label'], 'journeys': t.get('journeys', []), 'paises': t.get('paises', []),
                      'canales': t.get('canales', []), 'fecha_inicio': t.get('fecha_inicio', ''),
                      'campaigns': campaigns})
    return tests


# ---------------------------------------------------------------------------
# Matriz completa
# ---------------------------------------------------------------------------
CANALES_ORDEN = ['Push', 'Email']


def build_matrix(events_df, settings_config, tests=None):
    """Construye el payload de data/matrix.json.

    `events_df` viene en formato largo (queries/matrix_events.sql): journey x campaña x canal x país x
    dimensión x segmento. `journey_label` debe ser el label del TEST y `variante` el label de la campaña
    (push y email de una misma variante comparten label, y por lo tanto columna).
    `celdas`: una fila por dimensión x segmento x país x canal x test x campaña con impresiones y clics.
    El dashboard filtra por test/país/canal, calcula CTR = clics / impresiones x 100 por celda y compara
    cada campaña contra el CTR promedio del segmento dentro de su propio test.
    """
    settings = (settings_config or {}).get('settings') or {}
    tests = _estructura_tests(events_df, tests)

    df = events_df.copy()
    for col in ('delivered', 'clicked'):
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    for col in ('pais', 'canal', 'segmento'):
        df[col] = _texto(df[col])
    celdas, dimensiones = [], []
    for dim in DIMENSIONES:
        sub = df[df['dimension'] == dim['key']]
        if sub.empty:
            continue
        g = (sub.groupby(['segmento', 'pais', 'canal', 'journey_label', 'variante'])[['delivered', 'clicked']]
                .sum().reset_index())
        totales = g.groupby('segmento')['delivered'].sum().to_dict()
        dimensiones.append({'key': dim['key'], 'label': dim['label'], 'icon': dim['icon'],
                            'segmentos': _ordenar(list(totales), totales, dim['order'])})
        for r in g.itertuples(index=False):
            celdas.append({'d': dim['key'], 's': r.segmento, 'p': r.pais, 'c': r.canal, 'j': r.journey_label,
                           'v': r.variante, 'imp': int(r.delivered), 'clk': int(r.clicked)})

    df = df[df['dimension'] == 'country']   # totales: cada social cuenta una sola vez
    paises = df.groupby('pais')['delivered'].sum().sort_values(ascending=False).index.tolist()
    en_datos = set(df['canal'])
    canales = [c for c in CANALES_ORDEN if c in en_datos] + sorted(en_datos - set(CANALES_ORDEN))
    return {
        'tests': tests,
        'paises': paises,
        'canales': canales,
        'dimensiones': dimensiones,
        'celdas': celdas,
        'metadata': {
            'journeys': [t['label'] for t in tests],
            'total_impresiones': int(df['delivered'].sum()),
            'total_clics': int(df['clicked'].sum()),
            'fecha_calculo': datetime.now().astimezone().isoformat(timespec='seconds'),
            'min_delivered': int(settings.get('min_delivered', 0) or 0),
            'formula': 'CTR = (Socials con clic / Impresiones) x 100',
        },
    }


def resumen_ctr(events_df):
    """Tabla test x canal x variante con impresiones, clics y CTR % (para imprimir en consola)."""
    tot = events_df[events_df['dimension'] == 'country']
    g = tot.groupby(['journey_label', 'canal', 'variante'])[['delivered', 'clicked']].sum()
    g['ctr_%'] = (100 * g['clicked'] / g['delivered']).round(2)
    return g.rename(columns={'delivered': 'impresiones', 'clicked': 'clics'})

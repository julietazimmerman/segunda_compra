"""Cálculo de CTR por campaña y segmento, y construcción de la matriz para el dashboard.

    CTR = (Clics / Impresiones) x 100
        Impresiones = eventos push_delivered
        Clics       = eventos push_session

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
#   source: de qué tabla sale la dimensión (attrs / searches)
#   column: columna a agrupar luego del merge con push
#   order:  orden fijo de segmentos (None = por impresiones desc)
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


def _preparar_fuentes(push_df, attrs_df, searches_df):
    """Devuelve un DataFrame por fuente, ya mergeado con push y con las columnas de segmento listas.

    Todas las fuentes llevan `pais` (país del social) para el filtro de país del dashboard."""
    push = push_df[['social_id', 'journey_label', 'variante', 'delivered', 'clicked']].copy()
    push['delivered'] = pd.to_numeric(push['delivered'], errors='coerce').fillna(0).astype(int)
    push['clicked'] = pd.to_numeric(push['clicked'], errors='coerce').fillna(0).astype(int)

    # Atributos: 1 a 1 (salvaguarda por si la query devolviera más de una fila por social).
    attrs = attrs_df.drop_duplicates('social_id')
    a = push.merge(attrs, on='social_id', how='left')
    for col in ('traveler_type', 'card', 'status', 'country'):
        a[col] = _texto(a[col])
    a['pais'] = a['country']
    cupon = pd.to_numeric(a['used_cupon'], errors='coerce')
    a['used_cupon'] = cupon.map({1: 'Sí', 0: 'No'}).fillna(SIN_DATOS)
    a['uso_cuotas'] = pd.to_numeric(a['cantidad_cuotas'], errors='coerce').map(_uso_cuotas)

    # Búsquedas: LEFT JOIN, los socials sin búsquedas van al rango "0".
    base = a[['social_id', 'journey_label', 'variante', 'delivered', 'clicked', 'pais']]
    s = base.merge(searches_df, on='social_id', how='left')
    for col in ('nro_busquedas', 'destinos_distintos', 'productos_distintos'):
        s[col] = pd.to_numeric(s[col], errors='coerce').fillna(0)
    s['rango_busquedas'] = s['nro_busquedas'].map(_rango_busquedas)
    s['destinos_distintos'] = s['destinos_distintos'].map(lambda n: _cap(n, 5))
    s['productos_distintos'] = s['productos_distintos'].map(lambda n: _cap(n, 3))

    return {'attrs': a, 'searches': s}


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
        campaigns = [{'label': c['label'], 'mensaje': c.get('mensaje', '')} for c in t['campaigns']]
        conocidas = {c['label'] for c in campaigns}
        campaigns += [{'label': v, 'mensaje': ''} for v in sorted(con_datos[t['label']]) if v not in conocidas]
        tests.append({'label': t['label'], 'journeys': t.get('journeys', []), 'paises': t.get('paises', []),
                      'fecha_inicio': t.get('fecha_inicio', ''), 'campaigns': campaigns})
    return tests


# ---------------------------------------------------------------------------
# Matriz completa
# ---------------------------------------------------------------------------
def build_matrix(push_df, attrs_df, searches_df, settings_config, tests=None):
    """Construye el payload de data/matrix.json.

    `push_df.journey_label` debe ser el label del TEST (journeys de distintos países ya unificados).
    `celdas`: una fila por dimensión x segmento x país x test x campaña con impresiones y clics.
    El dashboard filtra por país/test, calcula CTR = clics / impresiones x 100 por celda y compara
    cada campaña contra el CTR promedio del segmento dentro de su propio test.
    """
    settings = (settings_config or {}).get('settings') or {}
    tests = _estructura_tests(push_df, tests)

    fuentes = _preparar_fuentes(push_df, attrs_df, searches_df)
    celdas, dimensiones = [], []
    for dim in DIMENSIONES:
        df = fuentes[dim['source']]
        if df.empty:
            continue
        col = dim['column']
        keys = [col, 'journey_label', 'variante'] if col == 'country' else [col, 'pais', 'journey_label', 'variante']
        g = df.groupby(keys)[['delivered', 'clicked']].sum().reset_index()
        if col == 'country':
            g['pais'] = g[col]
        totales = g.groupby(col)['delivered'].sum().to_dict()
        dimensiones.append({'key': dim['key'], 'label': dim['label'], 'icon': dim['icon'],
                            'segmentos': _ordenar(list(totales), totales, dim['order'])})
        for r in g.itertuples(index=False):
            celdas.append({'d': dim['key'], 's': getattr(r, col), 'p': r.pais, 'j': r.journey_label,
                           'v': r.variante, 'imp': int(r.delivered), 'clk': int(r.clicked)})

    paises = (fuentes['attrs'].groupby('pais')['delivered'].sum().sort_values(ascending=False).index.tolist())
    delivered = int(pd.to_numeric(push_df['delivered'], errors='coerce').fillna(0).sum())
    clicked = int(pd.to_numeric(push_df['clicked'], errors='coerce').fillna(0).sum())
    return {
        'tests': tests,
        'paises': paises,
        'dimensiones': dimensiones,
        'celdas': celdas,
        'metadata': {
            'journeys': [t['label'] for t in tests],
            'total_socials': int(push_df['social_id'].nunique()),
            'total_impresiones': delivered,
            'total_clics': clicked,
            'fecha_calculo': datetime.now().astimezone().isoformat(timespec='seconds'),
            'min_delivered': int(settings.get('min_delivered', 0) or 0),
            'formula': 'CTR = (Clics / Impresiones) x 100',
        },
    }


def resumen_ctr(push_df):
    """Tabla journey x campaña con impresiones, clics y CTR % (para imprimir en consola)."""
    g = push_df.groupby(['journey_label', 'journey_name', 'variante'])[['delivered', 'clicked']].sum()
    g['ctr_%'] = (100 * g['clicked'] / g['delivered']).round(2)
    return g.rename(columns={'delivered': 'impresiones', 'clicked': 'clics'})

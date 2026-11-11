#!/usr/bin/env python3
"""Baja del lake los eventos push de los tests definidos en tests.csv y los atributos de la audiencia,
y genera los JSON de data/ más la matriz de CTR (data/matrix.json + data/matrix.js).

    CTR = (Clics / Impresiones) x 100

Configuración:
    tests.csv              una fila por campaña: Pais-Marca, Fechas, Mensaje, journey_name, campaign_name, clt_c
                           (para agregar un test, agregar sus filas y volver a correr este script).
                           Los journeys que solo difieren en el país se unifican en un test (ver test_label);
                           columna opcional 'test' para forzar el nombre del test.
    config/campaigns.yaml  parámetros de la matriz (min_delivered)

Uso:
    python fetch_data.py               # consulta el lake (Trino) y recalcula todo
    python fetch_data.py --from-cache  # recalcula la matriz con los JSON ya guardados en data/

No usa datos ficticios: si no hay datos, no hay matriz.
"""
import argparse
import csv
import json
import os
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
QUERIES = ROOT / 'queries'
CONFIG = ROOT / 'config'
TESTS_CSV = ROOT / 'tests.csv'
TESTS_COLS = ['Pais-Marca', 'Fechas', 'Mensaje', 'journey_name', 'campaign_name', 'clt_c']

try:
    import pandas as pd
    import yaml
except ImportError as exc:
    raise SystemExit(
        "Faltan dependencias. Instalá pandas y pyyaml (pip install pandas pyyaml). "
        f"Detalle: {exc}"
    ) from exc

from metrics.calculate_index import build_matrix, resumen_ctr


# ---------------------------------------------------------------------------
# Conexión al lake: mismo patrón que build_data.py
# ---------------------------------------------------------------------------
# Usa OKR_CREDS_PATH si está definida; si no, el creds.env del usuario actual y, por último, el original.
CREDS_PATH = os.getenv('OKR_CREDS_PATH') or next(
    (str(c) for c in (Path.home() / 'Documents' / 'Credenciales' / 'creds.env',
                      Path(r"C:\Users\maria.terrier\Documents\Credenciales\creds.env")) if c.is_file()),
    str(Path.home() / 'Documents' / 'Credenciales' / 'creds.env'))


def query_trino(sqls):
    """Ejecuta una o varias queries ya renderizadas con el mismo conector y creds.env de Marketing.

    Devuelve una lista de resultados (lista de dicts), uno por query."""
    try:
        from dotenv import find_dotenv, load_dotenv
        from trino_despegar.connection import connect_to_datalake, disconnect
    except ImportError as exc:
        raise SystemExit(
            "Faltan dependencias para la conexión. Instalá python-dotenv y "
            "el paquete interno trino_despegar que utilizan los otros tableros. "
            f"Detalle: {exc}"
        ) from exc

    creds = Path(CREDS_PATH)
    if not creds.is_file():
        raise SystemExit(
            f"No encontré el archivo de credenciales: {CREDS_PATH}\n"
            "Revisá que exista en la misma ubicación que en los otros tableros."
        )

    dotenv_path = find_dotenv(CREDS_PATH)
    if not dotenv_path:
        raise SystemExit(f"No se pudo leer creds.env: {CREDS_PATH}")
    load_dotenv(dotenv_path, override=True)
    os.environ.setdefault('USER', os.environ.get('USERNAME') or 'maria.terrier')

    print('Credenciales:', CREDS_PATH)
    conn = None
    results = []
    try:
        conn = connect_to_datalake(dotenv_path, verify=os.getenv('VERIFY'))
        cursor = conn['cur']
        for i, sql in enumerate(sqls, 1):
            print(f'  Ejecutando query {i}/{len(sqls)}...')
            cursor.execute(sql)
            cols = [c[0] for c in cursor.description]
            results.append([dict(zip(cols, row)) for row in cursor.fetchall()])
        return results
    finally:
        if conn is not None:
            disconnect(conn)


def render_sql(sql, **params):
    for key, value in params.items():
        sql = sql.replace(f'__{key}__', value.isoformat() if isinstance(value, date) else str(value))
    return sql


# ---------------------------------------------------------------------------
# Configuración: tests.csv
# ---------------------------------------------------------------------------
def load_yaml(path):
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding='utf-8')) or {}


def journey_label(name):
    """'Hypercluster_PRECIO_FLEXIBILIDAD_AR_W44' -> 'PRECIO_FLEXIBILIDAD_AR_W44'."""
    return re.sub(r'^Hypercluster_', '', str(name))


def test_label(journey_name, pais_marca, override=''):
    """Nombre del test que agrupa journeys que solo difieren en el país.

    Se saca del journey_name el código de país de 'Pais-Marca' (ej. AR-D -> '_AR_'):
        Hypercluster_PRECIO_FLEXIBILIDAD_AR_W44 (AR-D)  -> PRECIO_FLEXIBILIDAD_W44
        Hypercluster_TipoDestino_AR_W45         (AR-D)  -> TipoDestino_W45
        Hypercluster_TipoDestino_W45            (Hispa) -> TipoDestino_W45
    Si tests.csv tiene una columna opcional 'test' con valor, se usa ese nombre.
    """
    if override:
        return override
    pais = (pais_marca or '').split('-')[0].strip().upper()
    name = str(journey_name)
    if re.fullmatch(r'[A-Z]{2}', pais):
        name = re.sub(rf'_{pais}(?=_|$)', '', name)
    return journey_label(name)


def group_tests(journeys):
    """Agrupa los journeys por test: campañas unificadas por label, países y fecha de inicio mínima."""
    tests = {}
    for j in journeys:
        t = tests.setdefault(j['test'], {'label': j['test'], 'journeys': [], 'paises': [],
                                         'fecha_inicio': j['fecha_inicio'], 'campaigns': []})
        t['journeys'].append(j['name'])
        if j['pais_marca'] and j['pais_marca'] not in t['paises']:
            t['paises'].append(j['pais_marca'])
        t['fecha_inicio'] = min(t['fecha_inicio'], j['fecha_inicio'])
        for c in j['campaigns']:
            if c['label'] not in {x['label'] for x in t['campaigns']}:
                t['campaigns'].append({'label': c['label'], 'mensaje': c['mensaje']})
    return list(tests.values())


def load_tests(path=TESTS_CSV):
    """Lee tests.csv y devuelve la lista de journeys (en orden de aparición), cada uno con sus campañas.

    - fecha_inicio del journey = la menor 'Fechas' de sus campañas
    - label de la campaña = lo que sigue al último '-' de clt_c (ej. '...-PrecioAlto' -> 'PrecioAlto')
    """
    if not path.is_file():
        raise SystemExit(f'No encontré {path.name}. Debe tener las columnas: ' + ', '.join(TESTS_COLS))
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        faltan = [c for c in TESTS_COLS if c not in (reader.fieldnames or [])]
        if faltan:
            raise SystemExit(f'{path.name}: faltan columnas {faltan}')
        rows = [{k: (v or '').strip() for k, v in r.items() if k} for r in reader]

    journeys = {}
    for r in rows:
        if not r['journey_name'] or not r['campaign_name']:
            continue
        j = journeys.setdefault(r['journey_name'], {
            'name': r['journey_name'], 'label': journey_label(r['journey_name']),
            'test': test_label(r['journey_name'], r['Pais-Marca'], r.get('test', '')),
            'pais_marca': r['Pais-Marca'], 'fecha_inicio': r['Fechas'], 'campaigns': []})
        if r['Fechas'] and (not j['fecha_inicio'] or r['Fechas'] < j['fecha_inicio']):
            j['fecha_inicio'] = r['Fechas']
        label = r['clt_c'].rsplit('-', 1)[-1] if r['clt_c'] else r['campaign_name']
        j['campaigns'].append({'campaign_name': r['campaign_name'], 'label': label, 'mensaje': r['Mensaje']})
    if not journeys:
        raise SystemExit(f'{path.name} no tiene tests.')
    for j in journeys.values():
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', j['fecha_inicio'] or ''):
            raise SystemExit(f"{path.name}: fecha inválida para {j['name']}: '{j['fecha_inicio']}' (usar YYYY-MM-DD)")
    return list(journeys.values())


def read_sql(name):
    return (QUERIES / name).read_text(encoding='utf-8')


def map_variante(raw, journeys):
    """campaign_name de Insider -> label de tests.csv. Si no está en el CSV, se devuelve el campaign_name."""
    if raw is None:
        return None
    raw = str(raw).strip()
    for j in journeys:
        for c in j['campaigns']:
            if c['campaign_name'] == raw:
                return c['label']
    return raw


def save_json(name, payload):
    (DATA / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding='utf-8')


def save_df(name, df):
    save_json(name, json.loads(df.to_json(orient='records', force_ascii=False, date_format='iso')))


def load_df(name):
    path = DATA / name
    if not path.is_file():
        raise SystemExit(f'No existe {path.relative_to(ROOT)}. Corré primero: python fetch_data.py')
    return pd.DataFrame(json.loads(path.read_text(encoding='utf-8')))


def sanity_checks(push, attrs, journeys):
    """Controles de granularidad y de cobertura de tests.csv."""
    dup_push = push.duplicated(['social_id', 'journey_name', 'variante_raw']).sum()
    dup_attrs = attrs['social_id'].duplicated().sum() if not attrs.empty else 0
    sin_delivered = int(((push['clicked'] > 0) & (push['delivered'] == 0)).sum())
    print('Controles:')
    print(f'  push duplicados (social x journey x campaña):  {dup_push}' + ('  <-- REVISAR' if dup_push else ''))
    print(f'  attrs con social_id repetido:                   {dup_attrs}' + ('  <-- REVISAR' if dup_attrs else ''))
    print(f'  socials con clic sin impresion registrada:      {sin_delivered:,}')
    conocidas = {c['campaign_name'] for j in journeys for c in j['campaigns']}
    en_datos = set(push['variante_raw'].dropna().unique())
    for raw in sorted(en_datos - conocidas):
        print(f'  campaña en Insider que no está en tests.csv (se muestra con su nombre completo): {raw}')
    for raw in sorted(conocidas - en_datos):
        print(f'  campaña de tests.csv sin eventos en Insider: {raw}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def fetch(journeys):
    journey_names = ', '.join("'" + j['name'].replace("'", "''") + "'" for j in journeys)
    fecha_min = min(j['fecha_inicio'] for j in journeys)
    params = dict(JOURNEY_NAMES=journey_names, FECHA_MIN=fecha_min)
    sqls = [render_sql(read_sql(n), **params) for n in
            ('push_analytics.sql', 'social_attrs.sql', 'social_searches.sql')]

    push_rows, attrs_rows, searches_rows = query_trino(sqls)

    push = pd.DataFrame(push_rows, columns=['social_id', 'journey_name', 'variante_raw', 'delivered', 'clicked'])
    # La query lee desde la fecha mínima de todos los tests; un journey no tiene eventos
    # antes de su propia fecha de inicio, así que no hace falta filtrar por journey.
    label_push(push, journeys)

    attrs = pd.DataFrame(attrs_rows, columns=['social_id', 'country', 'brand', 'status', 'card', 'used_cupon',
                                              'cantidad_cuotas', 'traveler_type'])
    searches = pd.DataFrame(searches_rows, columns=['social_id', 'nro_busquedas', 'destinos_distintos',
                                                    'productos_distintos'])

    DATA.mkdir(exist_ok=True)
    save_df('push_events.json', push)
    save_df('social_attrs.json', attrs)
    save_df('social_searches.json', searches)
    return push, attrs, searches


def label_push(push, journeys):
    """journey_label = nombre del TEST (journeys de distintos países unificados); variante = label de la campaña."""
    push['journey_label'] = push['journey_name'].map({j['name']: j['test'] for j in journeys})
    push['variante'] = push['variante_raw'].map(lambda r: map_variante(r, journeys))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--from-cache', action='store_true',
                        help='No consulta el lake: recalcula la matriz con los JSON de data/')
    args = parser.parse_args()

    journeys = load_tests()
    settings = load_yaml(CONFIG / 'campaigns.yaml')

    print('=' * 65)
    print('INSIDER ARCHITECT - CTR POR CAMPAÑA Y AUDIENCIA')
    print('=' * 65)
    tests = group_tests(journeys)
    print(f"tests.csv: {len(journeys)} journeys -> {len(tests)} tests")
    for t in tests:
        print(f"  {t['label']}: {', '.join(t['paises'])} | campañas: {', '.join(c['label'] for c in t['campaigns'])}")

    if args.from_cache:
        print('Fuente: cache en data/')
        push, attrs, searches = (load_df(n) for n in (
            'push_events.json', 'social_attrs.json', 'social_searches.json'))
        if 'journey_name' not in push.columns:
            raise SystemExit('El cache de data/ es de una versión anterior. Corré: python fetch_data.py')
        # Re-mapea por si cambió tests.csv; los journeys que ya no están en el CSV se descartan.
        push = push[push['journey_name'].isin({j['name'] for j in journeys})].copy()
        label_push(push, journeys)
    else:
        print('Fuente: Trino / sot.insider_mobile_app_events + tmp.socials_*_2nd_trip')
        push, attrs, searches = fetch(journeys)

    if push.empty:
        raise SystemExit('No se encontraron eventos push para los journeys de tests.csv. No se genera matriz.')

    sanity_checks(push, attrs, journeys)

    matrix = build_matrix(push, attrs, searches, settings, tests=tests)
    DATA.mkdir(exist_ok=True)
    save_json('matrix.json', matrix)
    # Copia como JS para que index.html funcione abierto desde el disco (file://), donde fetch() está bloqueado.
    (DATA / 'matrix.js').write_text(
        'window.MATRIX_DATA = ' + json.dumps(matrix, ensure_ascii=False) + ';\n', encoding='utf-8')

    print('-' * 65)
    print(f"Socials unicos:      {matrix['metadata']['total_socials']:,}")
    print(f"Impresiones / clics: {matrix['metadata']['total_impresiones']:,} / {matrix['metadata']['total_clics']:,}")
    print('CTR = (Clics / Impresiones) x 100 por test, journey y campaña:')
    print(resumen_ctr(push).to_string())
    print(f"Celdas de la matriz: {len(matrix['celdas']):,} (min_delivered={matrix['metadata']['min_delivered']})")
    print('OK: data/push_events.json, social_attrs.json, social_searches.json, matrix.json, matrix.js')


if __name__ == '__main__':
    main()

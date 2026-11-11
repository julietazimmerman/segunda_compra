#!/usr/bin/env python3
"""Baja del lake los eventos PUSH y EMAIL de los tests definidos en tests.csv, cruzados con los atributos
de la audiencia y agregados por perfil (data/events.json), y genera la matriz de CTR
(data/matrix.json + data/matrix.js).

    CTR = (Socials con clic / Impresiones) x 100
    Push:  push_delivered / push_session   ·   Email: email_delivered / email_click

Configuración:
    tests.csv              una fila por campaña. Columnas (ver TESTS_COLUMNS, acepta alias y mayúsculas):
                             Pais-Marca, Fechas, journey_name, campaign_name_push   (obligatorias)
                             Test, Mensaje, clt_c, campaign_name_email               (opcionales)
                           Test     -> agrupa las variantes que se comparan: 'Test · Wnn' (ver test_label).
                           Mensaje  -> nombre de la variante (columna de la matriz).
                           campaign_name_push / campaign_name_email -> campañas de cada canal; una
                           variante puede tener las dos (van a la misma columna, el dashboard filtra
                           por canal). 'Email 1' / 'App Push 2' se completan con el journey_name adelante.
                           Para actualizar: reemplazar el CSV y correr con --check para revisar la agrupación.
    config/campaigns.yaml  parámetros de la matriz (min_delivered)

Uso:
    python fetch_data.py --check       # solo valida tests.csv y muestra los tests que arma
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

try:
    import pandas as pd
    import yaml
except ImportError as exc:
    raise SystemExit(
        "Faltan dependencias. Instalá pandas y pyyaml (pip install pandas pyyaml). "
        f"Detalle: {exc}"
    ) from exc

from metrics.calculate_index import a_formato_largo, build_matrix, resumen_ctr


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


def country_codes(pais_marca):
    """'AR-D' -> ['AR'] · 'CO y PE - D' -> ['CO', 'PE'] · 'Hispa-D' / 'BEDA' -> []."""
    return re.findall(r'\b[A-Z]{2}\b', (pais_marca or '').rsplit('-', 1)[0])


def journey_sin_pais(journey_name, pais_marca):
    """Saca del journey_name los códigos de país de 'Pais-Marca', para unificar journeys de varios países:
        Hypercluster_PRECIO_FLEXIBILIDAD_AR_W44  (AR-D)        -> PRECIO_FLEXIBILIDAD_W44
        Hypercluster_DestinoRepetido_COPE_W45    (CO y PE - D) -> DestinoRepetido_W45
        Hypercluster_TipoDestino_W45             (Hispa-D)     -> TipoDestino_W45
    """
    name = str(journey_name)
    codes = country_codes(pais_marca)
    for code in ([''.join(codes)] if len(codes) > 1 else []) + codes:
        name = re.sub(rf'_{code}(?=_|$)', '', name)
    return journey_label(name)


def wave_of(journey_name, fecha):
    """Semana del test: el '_Wnn' del journey_name o, si no tiene, la semana ISO de la fecha."""
    m = re.search(r'_(W\d+)(?=_|$)', str(journey_name))
    return m.group(1) if m else f'W{date.fromisoformat(fecha).isocalendar()[1]}'


def test_label(row):
    """Nombre del test (lo que se compara entre sí en la matriz).

    - Con columna 'Test': 'Test · Wnn' (ej. 'Sensibilidad al precio · W44'). Así un mismo journey puede
      tener varios tests (PRECIO_FLEXIBILIDAD tiene Sensibilidad al precio y Flexibilidad) y un mismo
      test repetido en otra semana queda separado.
    - Sin 'Test': el journey sin país (comportamiento anterior).
    """
    if row['test']:
        return f"{' '.join(row['test'].split())} · {wave_of(row['journey_name'], row['fecha'])}"
    return journey_sin_pais(row['journey_name'], row['pais_marca'])


# Columnas de tests.csv -> nombre interno. Se aceptan varios nombres (sin importar mayúsculas)
# para que el CSV pueda cambiar de versión sin tocar el código.
TESTS_COLUMNS = {
    'pais_marca':     ['Pais-Marca', 'País-Marca', 'pais_marca'],
    'fecha':          ['Fechas', 'Fecha', 'fecha_inicio'],
    'test':           ['Test'],
    'mensaje':        ['Mensaje', 'Variante'],
    'journey_name':   ['journey_name'],
    'campaign_push':  ['campaign_name_push', 'campaign_name'],
    'campaign_email': ['campaign_name_email'],
    'clt_c':          ['clt_c'],
}
TESTS_REQUIRED = ['pais_marca', 'fecha', 'journey_name', 'campaign_push']
CANALES = {'campaign_push': 'Push', 'campaign_email': 'Email'}


def _resolver_columnas(fieldnames, path):
    norm = {str(f).strip().lower(): f for f in fieldnames or [] if f}
    cols = {}
    for key, aliases in TESTS_COLUMNS.items():
        cols[key] = next((norm[a.lower()] for a in aliases if a.lower() in norm), None)
    faltan = [TESTS_COLUMNS[k][0] for k in TESTS_REQUIRED if not cols[k]]
    if faltan:
        raise SystemExit(f'{path.name}: faltan columnas {faltan}. Columnas encontradas: {list(fieldnames or [])}')
    return cols


def nombre_completo(campaign_name, journey_name):
    """Completa nombres abreviados en el CSV: 'Email 1' / 'App Push 2' -> '<journey_name> Email 1'.
    En Insider la campaña siempre lleva el journey adelante."""
    if re.fullmatch(r'(Email|App Push) \d+', campaign_name):
        return f'{journey_name} {campaign_name}'
    return campaign_name


def load_tests(path=TESTS_CSV, verbose=True):
    """Lee tests.csv y devuelve las campañas (push y email) en orden de aparición.

    Cada fila puede generar hasta dos campañas: una Push (campaign_name_push) y una Email
    (campaign_name_email), las dos con la misma variante. Cada campaña:
        journey_name, campaign_name, canal, test, label, mensaje, pais_marca, fecha
    - label (columna de la matriz) = 'Mensaje'; si está vacío, lo que sigue al último '-' de clt_c;
      y si no, el campaign_name.
    """
    if not path.is_file():
        raise SystemExit(f'No encontré {path.name}. Debe tener las columnas: '
                         + ', '.join(TESTS_COLUMNS[k][0] for k in TESTS_REQUIRED))
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        cols = _resolver_columnas(reader.fieldnames, path)
        rows = [{k: (r.get(c) or '').strip() if c else '' for k, c in cols.items()} for r in reader]

    campaigns, vistos, avisos = [], set(), []
    for n, r in enumerate(rows, 2):   # n = número de línea en el CSV (1 = encabezado)
        if not r['journey_name']:
            continue
        if not r['campaign_push'] and not r['campaign_email']:
            avisos.append(f"línea {n}: sin campaña push ni email, se ignora ({r['journey_name']} / {r['mensaje']})")
            continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['fecha']):
            raise SystemExit(f"{path.name} línea {n}: fecha inválida '{r['fecha']}' (usar YYYY-MM-DD)")
        clt_suffix = r['clt_c'].rsplit('-', 1)[-1] if '-' in r['clt_c'] else ''
        for col, canal in CANALES.items():
            if not r[col]:
                continue
            name = nombre_completo(r[col], r['journey_name'])
            if name != r[col]:
                avisos.append(f"línea {n}: '{r[col]}' se completa como '{name}'")
            if (r['journey_name'], canal, name) in vistos:
                continue
            vistos.add((r['journey_name'], canal, name))
            campaigns.append({
                'journey_name': r['journey_name'], 'campaign_name': name, 'canal': canal,
                'test': test_label(r), 'label': r['mensaje'] or clt_suffix or name,
                'mensaje': r['mensaje'], 'pais_marca': r['pais_marca'], 'fecha': r['fecha'],
            })
    if not campaigns:
        raise SystemExit(f'{path.name} no tiene campañas.')
    if verbose and avisos:
        print(f'{path.name}:')
        for a in avisos:
            print('  ' + a)
    return campaigns


def group_tests(campaigns):
    """Agrupa las campañas por test: columnas unificadas por label (push y email de la misma variante
    van a la misma columna), journeys, países, canales y fecha de inicio."""
    tests = {}
    for c in campaigns:
        t = tests.setdefault(c['test'], {'label': c['test'], 'journeys': [], 'paises': [], 'canales': [],
                                         'fecha_inicio': c['fecha'], 'campaigns': []})
        for key, value in (('journeys', c['journey_name']), ('paises', c['pais_marca']), ('canales', c['canal'])):
            if value and value not in t[key]:
                t[key].append(value)
        t['fecha_inicio'] = min(t['fecha_inicio'], c['fecha'])
        camp = next((x for x in t['campaigns'] if x['label'] == c['label']), None)
        if camp is None:
            camp = {'label': c['label'], 'mensaje': c['mensaje'], 'canales': []}
            t['campaigns'].append(camp)
        if c['canal'] not in camp['canales']:
            camp['canales'].append(c['canal'])
    return list(tests.values())


def check_tests(campaigns, tests):
    """Avisos sobre tests.csv que no cortan la ejecución pero conviene revisar."""
    avisos = []
    por_nombre = {}
    for c in campaigns:
        por_nombre.setdefault((c['canal'], c['campaign_name']), set()).add(c['journey_name'])
    for (canal, name), js in sorted(por_nombre.items()):
        if len(js) > 1:
            avisos.append(f"campaña {canal} repetida en varios journeys (¿copy-paste?): {name} -> {', '.join(sorted(js))}")
    for t in tests:
        if len(t['campaigns']) < 2:
            avisos.append(f"test con una sola variante (no hay contra qué comparar): {t['label']}")
    if avisos:
        print('Avisos de tests.csv:')
        for a in avisos:
            print('  ' + a)


def read_sql(name):
    return (QUERIES / name).read_text(encoding='utf-8')


def save_json(name, payload):
    (DATA / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding='utf-8')


def save_df(name, df):
    save_json(name, json.loads(df.to_json(orient='records', force_ascii=False, date_format='iso')))


def load_df(name):
    path = DATA / name
    if not path.is_file():
        raise SystemExit(f'No existe {path.relative_to(ROOT)}. Corré primero: python fetch_data.py')
    return pd.DataFrame(json.loads(path.read_text(encoding='utf-8')))


# Salida de queries/matrix_events.sql (formato largo: una fila por journey x campaña x canal x país x
# dimensión x segmento; la dimensión 'country' es el total, cada social cuenta una vez).
EVENT_COLS = ['journey_name', 'variante_raw', 'canal', 'pais', 'dimension', 'segmento',
              'delivered', 'clicked', 'socials', 'clic_sin_imp']


def sanity_checks(events, campaigns):
    """Controles de cobertura de tests.csv contra lo que hay en Insider (por journey x canal x campaña)."""
    print('Controles:')
    for canal, g in events[events['dimension'] == 'country'].groupby('canal'):
        print(f"  {canal:5}: {int(g['delivered'].sum()):>12,} impresiones · {int(g['clicked'].sum()):>9,} socials con clic"
              f" · {int(g['clic_sin_imp'].sum()):,} con clic sin impresión registrada")
    conocidas = {(c['journey_name'], c['canal'], c['campaign_name']) for c in campaigns}
    en_datos = set(events[['journey_name', 'canal', 'variante_raw']].dropna().itertuples(index=False, name=None))
    asignadas = set(events.loc[events['journey_label'].notna(), ['journey_name', 'canal', 'variante_raw']]
                    .dropna().itertuples(index=False, name=None))
    for j, canal, raw in sorted(en_datos - conocidas):
        destino = 'se suma a su test' if (j, canal, raw) in asignadas else 'NO entra en la matriz'
        print(f'  campaña {canal} en Insider que no está en tests.csv ({destino}): {j} / {raw}')
    con_datos = {(j, canal) for j, canal, _ in en_datos}
    for j, canal, raw in sorted(conocidas - en_datos):
        if (j, canal) in con_datos:   # los journeys sin ningún evento del canal se informan abajo
            print(f'  campaña {canal} de tests.csv sin eventos en Insider: {j} / {raw}')
    for j, canal in sorted({(j, canal) for j, canal, _ in conocidas} - con_datos):
        print(f'  journey de tests.csv sin ningún evento {canal}: {j}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def sql_list(values):
    """Lista SQL de strings; si está vacía devuelve '' para que el IN () siga siendo válido."""
    values = list(dict.fromkeys(values))
    return ', '.join("'" + v.replace("'", "''") + "'" for v in values) if values else "''"


def fetch(campaigns):
    """Una sola query: push + email, cruzados con la audiencia y agregados por perfil (ver matrix_events.sql)."""
    sql = render_sql(
        read_sql('matrix_events.sql'),
        PUSH_JOURNEYS=sql_list(c['journey_name'] for c in campaigns if c['canal'] == 'Push'),
        EMAIL_JOURNEYS=sql_list(c['journey_name'] for c in campaigns if c['canal'] == 'Email'),
        FECHA_MIN=min(c['fecha'] for c in campaigns))
    # La query lee desde la fecha mínima de todos los tests; un journey no tiene eventos
    # antes de su propia fecha de inicio, así que no hace falta filtrar por journey.
    (rows,) = query_trino([sql])
    events = pd.DataFrame(rows, columns=EVENT_COLS)
    DATA.mkdir(exist_ok=True)
    save_df('events.json', events)
    return events


def events_desde_cache_viejo():
    """Convierte el cache por social de versiones anteriores (solo push) al formato agregado de events.json."""
    push, attrs, searches = (load_df(n) for n in ('push_events.json', 'social_attrs.json', 'social_searches.json'))
    push = push[['social_id', 'journey_name', 'variante_raw', 'delivered', 'clicked']].copy()
    push['canal'] = 'Push'
    push['clicked'] = pd.to_numeric(push['clicked'], errors='coerce').fillna(0).clip(upper=1)
    df = push.merge(attrs.drop_duplicates('social_id'), on='social_id', how='inner')
    df = df.merge(searches, on='social_id', how='left')
    caps = {'nro_busquedas': 11, 'destinos_distintos': 5, 'productos_distintos': 3, 'cantidad_cuotas': 2}
    for col, tope in caps.items():
        df[col] = pd.to_numeric(df[col], errors='coerce')
        if col != 'cantidad_cuotas':
            df[col] = df[col].fillna(0)
        df[col] = df[col].clip(upper=tope)
    df['socials'] = 1
    df['clic_sin_imp'] = ((df['clicked'] > 0) & (df['delivered'] == 0)).astype(int)
    return a_formato_largo(df)[EVENT_COLS]


def load_cache(campaigns):
    if (DATA / 'events.json').is_file():
        events = load_df('events.json')
        if 'dimension' not in events.columns:
            raise SystemExit('data/events.json es de una versión anterior. Borralo o corré: python fetch_data.py')
    elif (DATA / 'push_events.json').is_file():
        print('  Cache de una versión anterior (solo push, por social): se convierte a data/events.json.')
        events = events_desde_cache_viejo()
        save_df('events.json', events)
        print('  Ya se pueden borrar data/push_events.json, social_attrs.json y social_searches.json.')
    else:
        raise SystemExit('No hay cache en data/. Corré primero: python fetch_data.py')
    print('  (los journeys que figuran "sin ningún evento" pueden ser simplemente nuevos: correr sin --from-cache)')
    # Re-mapea por si cambió tests.csv; los journeys que ya no están en el CSV se descartan.
    return events[events['journey_name'].isin({c['journey_name'] for c in campaigns})].copy()


def label_events(events, campaigns):
    """Asigna a cada fila su test (journey_label) y su variante (label de la campaña),
    por journey + canal + campaña. Push y email de la misma variante quedan en la misma columna.

    Campañas de Insider que no están en tests.csv: si su journey tiene un solo test, se suman a ese test
    con su nombre completo; si el journey tiene varios tests no se sabe a cuál van y se descartan."""
    por_campania = {(c['journey_name'], c['canal'], c['campaign_name']): c for c in campaigns}
    tests_por_journey = {}
    for c in campaigns:
        tests_por_journey.setdefault(c['journey_name'], set()).add(c['test'])

    def asignar(j, canal, raw):
        raw = None if raw is None or pd.isna(raw) else str(raw).strip()
        c = por_campania.get((j, canal, raw))
        if c:
            return c['test'], c['label']
        ts = tests_por_journey.get(j, set())
        return (next(iter(ts)), raw) if len(ts) == 1 and raw else (None, None)

    asignado = [asignar(j, k, r) for j, k, r in zip(events['journey_name'], events['canal'], events['variante_raw'])]
    events['journey_label'] = [a[0] for a in asignado]
    events['variante'] = [a[1] for a in asignado]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--from-cache', action='store_true',
                        help='No consulta el lake: recalcula la matriz con data/events.json')
    parser.add_argument('--check', action='store_true',
                        help='Solo valida tests.csv y muestra cómo agrupa los tests (no consulta nada)')
    args = parser.parse_args()

    print('=' * 65)
    print('INSIDER ARCHITECT - CTR POR CAMPAÑA Y AUDIENCIA (PUSH + EMAIL)')
    print('=' * 65)
    campaigns = load_tests()
    settings = load_yaml(CONFIG / 'campaigns.yaml')
    tests = group_tests(campaigns)
    n_journeys = len({c['journey_name'] for c in campaigns})
    por_canal = ', '.join(f"{sum(c['canal'] == k for c in campaigns)} {k.lower()}" for k in CANALES.values())
    print(f"tests.csv: {len(campaigns)} campañas ({por_canal}), {n_journeys} journeys -> {len(tests)} tests")
    for t in tests:
        camps = ', '.join(f"{c['label']} [{'+'.join(c['canales'])}]" for c in t['campaigns'])
        print(f"  {t['label']}: {', '.join(t['paises'])} | {camps}")
    check_tests(campaigns, tests)
    if args.check:
        return

    if args.from_cache:
        print('Fuente: cache en data/')
        events = load_cache(campaigns)
    else:
        print('Fuente: Trino / sot.insider_mobile_app_events + sot.insider_email_events + tmp.socials_*_2nd_trip')
        events = fetch(campaigns)

    if events.empty:
        raise SystemExit('No se encontraron eventos para los journeys de tests.csv. No se genera matriz.')

    label_events(events, campaigns)
    sanity_checks(events, campaigns)
    events = events[events['journey_label'].notna()].copy()

    matrix = build_matrix(events, settings, tests=tests)
    DATA.mkdir(exist_ok=True)
    save_json('matrix.json', matrix)
    # Copia como JS para que index.html funcione abierto desde el disco (file://), donde fetch() está bloqueado.
    (DATA / 'matrix.js').write_text(
        'window.MATRIX_DATA = ' + json.dumps(matrix, ensure_ascii=False) + ';\n', encoding='utf-8')

    print('-' * 65)
    print(f"Impresiones / clics: {matrix['metadata']['total_impresiones']:,} / {matrix['metadata']['total_clics']:,}")
    print('CTR = (Socials con clic / Impresiones) x 100 por test, canal y variante:')
    print(resumen_ctr(events).to_string())
    print(f"Celdas de la matriz: {len(matrix['celdas']):,} (min_delivered={matrix['metadata']['min_delivered']})")
    print('OK: data/events.json, matrix.json, matrix.js')


if __name__ == '__main__':
    main()

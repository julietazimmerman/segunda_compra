#!/usr/bin/env python3
"""Genera data/hispa.json, data/brasil.json y data/summary.json.

Fuentes: Trino o un CSV exportado de la query SQL.
No usa datos ficticios. Solo muestra TY/LY cuando los datos han sido ejecutados.
"""
import argparse
import csv
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MONTHS = [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]
COUNTRIES = {'AR', 'CL', 'CO', 'EC', 'MX', 'PE', 'BR'}
TARGET = 611131
SEMESTER_START = date(2026, 10, 1)
KR_CATEGORIES = ('Activo', 'Dormido', 'Nuevo en el half')  # lo que cuenta para el KR; el resto va aparte
BRAND_ALIASES = {'Viajanet Passagens': 'Viajanet'}  # el Excel de targets trae una sola marca Viajanet


def month_after(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def month_end(y, m):
    yn, mn = month_after(y, m)
    return date(yn, mn, 1) - timedelta(days=1)


def week_start(day):
    """Lunes de la semana ISO (lunes a domingo) que contiene `day`."""
    return day - timedelta(days=day.weekday())


def last_closed_sunday(today):
    """Último domingo anterior a `today`: un lunes devuelve el domingo previo (semana cerrada)."""
    return today - timedelta(days=(today.weekday() + 1) % 7 or 7)


def ty_equivalent(day):
    """Fecha calendario TY equivalente a una fecha LY (mismo mes y día, +1 año)."""
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29/feb
        return date(day.year + 1, day.month, 28)


def build_weeks(cutoff):
    """Semanas lunes-domingo del semestre TY, recortadas al semestre y al corte."""
    first, last = date(2026, 10, 1), min(cutoff, date(2027, 3, 31))
    weeks, monday = [], week_start(first)
    while monday <= last:
        start, end = max(monday, first), min(monday + timedelta(days=6), last)
        weeks.append({'week': monday.isoformat(), 'label': f"S{monday.isocalendar()[1]} · {start:%d/%m}–{end:%d/%m}",
                      'start': start.isoformat(), 'end': end.isoformat()})
        monday += timedelta(days=7)
    return weeks


def parse_date(value):
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    return date.fromisoformat(str(value)[:10])


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
        for sql in sqls:
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


def from_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        required = {'social_id', 'country_code', 'second_purchase_date'}
        if not required.issubset(reader.fieldnames or []):
            raise SystemExit('El CSV debe tener: ' + ', '.join(sorted(required)))
        return list(reader)


def category_of(first_purchase, second_purchase, period_start):
    """Categoría de la base ANTES de la segunda compra (base al inicio del half)."""
    if first_purchase is None:
        return 'Sin clasificar'
    if first_purchase >= period_start:
        return 'Nuevo en el half' if first_purchase <= second_purchase else 'Sin clasificar'
    # Umbrales de 18 y 36 meses anclados al inicio del semestre.
    def months_before(start, n):
        year = start.year
        month = start.month - n
        while month <= 0:
            year -= 1
            month += 12
        import calendar
        return date(year, month, min(start.day, calendar.monthrange(year,month)[1]))
    cutoff_18 = months_before(period_start, 18)
    cutoff_36 = months_before(period_start, 36)
    # Activo: [inicio-18m, inicio). Dormido: [inicio-36m, inicio-18m). El borde de 18 meses es Activo.
    if first_purchase >= cutoff_18:
        return 'Activo'
    if first_purchase >= cutoff_36:
        return 'Dormido'
    return 'Fuera de base (>36 meses)'


def build(rows, cutoff):
    # Comparación MTD solo para el mes en curso; meses completos se comparan completos.
    # Nunca mostramos meses futuros ni extrapolamos targets.
    unique = {}
    for row in rows:
        sid = str(row.get('social_id') or '').strip()
        country = str(row.get('country_code') or '').strip().upper()
        if not sid or country not in COUNTRIES:
            continue
        purchase_day = parse_date(row['second_purchase_date'])
        region = 'Brasil' if country == 'BR' else 'Hispa'
        key = (region, sid, purchase_day.year)  # maximo un segundo viaje por anio
        candidate = dict(row, brand=BRAND_ALIASES.get(str(row.get('brand') or '').strip(), str(row.get('brand') or '').strip()), country_code=country, region=region, second_purchase_date=purchase_day, first_purchase_date=(parse_date(row['first_purchase_date']) if row.get('first_purchase_date') else None))
        if key not in unique or purchase_day < unique[key]['second_purchase_date']:
            unique[key] = candidate

    payloads = {}
    for region in ('Hispa', 'Brasil'):
        monthly = []
        entity_counts = {}
        category_counts = {}
        category_detail = {}
        out_detail = {}   # Fuera de base / sin clasificar: no cuentan para el KR, se informan aparte
        week_counts = {}
        for y, m in MONTHS:
            month_start = date(y, m, 1)
            limit = min(month_end(y, m), cutoff) if month_start <= cutoff else None
            if limit is None:
                monthly.append({'month': f'{y:04d}-{m:02d}', 'ty': None, 'ly': None})
                continue
            ly_y = y - 1
            ly_limit = date(ly_y, m, min(limit.day, month_end(ly_y, m).day))
            ty_rows = [x for x in unique.values() if x['region'] == region and month_start <= x['second_purchase_date'] <= limit]
            ly_rows = [x for x in unique.values() if x['region'] == region and date(ly_y, m, 1) <= x['second_purchase_date'] <= ly_limit]
            keymonth = f'{y:04d}-{m:02d}'
            counted = {'ty': 0, 'ly': 0}
            for year_type, period_rows, base_year in [('ty', ty_rows, y), ('ly', ly_rows, ly_y)]:
                for record in period_rows:
                    day = record['second_purchase_date']
                    country = record['country_code']
                    brand = str(record.get('brand') or 'Sin dato')
                    # Semana calendario TY; las compras LY se asignan a la semana de su fecha equivalente
                    # (mismo mes y día), así la suma semanal reconcilia exacto con el mes.
                    week = week_start(day if year_type == 'ty' else ty_equivalent(day)).isoformat()
                    cat = category_of(record.get('first_purchase_date'), day, date(base_year if m >= 10 else base_year - 1, 10, 1))
                    if cat not in KR_CATEGORIES:
                        k = (keymonth, week, country, brand, cat, year_type)
                        out_detail[k] = out_detail.get(k, 0) + 1
                        continue
                    counted[year_type] += 1
                    week_counts[(week, year_type)] = week_counts.get((week, year_type), 0) + 1
                    category_counts[(keymonth, cat, year_type)] = category_counts.get((keymonth, cat, year_type), 0) + 1
                    category_detail[(keymonth, week, country, brand, cat, year_type)] = category_detail.get((keymonth, week, country, brand, cat, year_type), 0) + 1
                    k = (keymonth, week, country, brand, year_type)
                    entity_counts[k] = entity_counts.get(k, 0) + 1
            monthly.append({'month': keymonth, 'ty': counted['ty'], 'ly': counted['ly']})
        payloads[region] = {
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'as_of': cutoff.isoformat(),  # el HTML prorratea el target MTD con esta fecha, no con la de ejecución
            'ty': sum(x['ty'] for x in monthly if x['ty'] is not None),
            'ly': sum(x['ly'] for x in monthly if x['ly'] is not None),
            'months': monthly,
            'repurchase_rate': None,  # Falta definición de base elegible
            'source': 'lake.clientes_trips_fact_full',
            'weeks': [{**w, 'ty': week_counts.get((w['week'],'ty'),0), 'ly': week_counts.get((w['week'],'ly'),0)}
                      for w in build_weeks(cutoff)],
            'breakdown': [{'month': k[0], 'week': k[1], 'country': k[2], 'brand': k[3],
                           'ty': entity_counts.get((*k[:4], 'ty'), 0),
                           'ly': entity_counts.get((*k[:4], 'ly'), 0)}
                          for k in sorted({k[:4] for k in entity_counts})],
            'categories': [{'month': month,'category': category,
                            'ty': category_counts.get((month,category,'ty'),0),
                            'ly': category_counts.get((month,category,'ly'),0)}
                           for month,category in sorted({k[:2] for k in category_counts})],
            'category_breakdown': [{'month': k[0], 'week': k[1], 'country': k[2], 'brand': k[3], 'category': k[4],
                                    'ty':category_detail.get((*k,'ty'),0),
                                    'ly':category_detail.get((*k,'ly'),0)}
                                    for k in sorted({k[:5] for k in category_detail})],
            'out_of_base': [{'month': k[0], 'week': k[1], 'country': k[2], 'brand': k[3], 'category': k[4],
                             'ty': out_detail.get((*k, 'ty'), 0), 'ly': out_detail.get((*k, 'ly'), 0)}
                            for k in sorted({k[:5] for k in out_detail})],
            'out_of_base_ty': sum(v for k, v in out_detail.items() if k[5] == 'ty'),
        }
    for region, payload in payloads.items():
        check_reconciliation(region, payload)
    return payloads


def check_reconciliation(region, payload):
    """Corta el build si meses, semanas, países/marcas y categorías no suman lo mismo."""
    for yt in ('ty', 'ly'):
        totals = {
            'meses': sum(x[yt] or 0 for x in payload['months']),
            'semanas': sum(x[yt] for x in payload['weeks']),
            'país/marca': sum(x[yt] for x in payload['breakdown']),
            'categorías': sum(x[yt] for x in payload['category_breakdown']),
        }
        if len(set(totals.values())) != 1:
            raise SystemExit(f'{region} {yt.upper()} no reconcilia: {totals}')


def build_audience(rows, cutoff, kr_payloads):
    """Base elegible agregada (query audiencia_base.sql) + cruce contra los recompradores del KR."""
    out = []
    for row in rows:
        brand = str(row.get('brand') or 'Sin dato').strip()
        out.append({
            'region': row['region'], 'country': row['country'],
            'brand': BRAND_ALIASES.get(brand, brand),
            'product': row['product'], 'platform': row['platform'], 'status': row['status'],
            'eligible': int(row['eligible']), 'rebuyers': int(row['rebuyers']),
        })
    # Control: los recompradores dentro de la base deben coincidir con los del KR (mismas 3 categorías).
    # Pueden diferir en unos pocos casos: la base asigna región por el país del 1er viaje y el KR por el del 2do.
    check = []
    for region in ('Hispa', 'Brasil'):
        for status in KR_CATEGORIES:
            if status == 'Nuevo en el half' and cutoff < SEMESTER_START:
                continue
            base = sum(x['rebuyers'] for x in out if x['region'] == region and x['status'] == status)
            kr = sum(c['ty'] for c in kr_payloads[region]['categories'] if c['category'] == status)
            check.append({'region': region, 'status': status, 'rebuyers_base': base, 'rebuyers_kr': kr,
                          'diff_pct': round((base / kr - 1) * 100, 2) if kr else None})
    return {
        'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'as_of': cutoff.isoformat(),
        'semester_start': SEMESTER_START.isoformat(),
        'source': 'lake.clientes_trips_fact_full',
        'rows': out,
        'check_vs_kr': check,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--csv', help='CSV exportado de queries/q1_second_purchase.sql')
    group.add_argument('--trino', action='store_true', help='Ejecutar SQL en Trino')
    parser.add_argument('--audience-csv', help='CSV exportado de queries/audiencia_base.sql (con --csv)')
    parser.add_argument('--as-of', default=last_closed_sunday(date.today()).isoformat(),
                        help='Corte TY en YYYY-MM-DD (por defecto, el último domingo cerrado)')
    args = parser.parse_args()
    cutoff = parse_date(args.as_of)
    if cutoff < SEMESTER_START:
        raise SystemExit('El semestre todavía no comenzó.')
    start = date(2025, 10, 1)
    end = min(cutoff + timedelta(days=1), date(2027, 4, 1))
    kr_sql = render_sql((ROOT / 'queries/q1_second_purchase.sql').read_text(encoding='utf-8'),
                        START_DATE=start, END_EXCLUSIVE=end)
    aud_sql = render_sql((ROOT / 'queries/audiencia_base.sql').read_text(encoding='utf-8'),
                         SEM_START=SEMESTER_START, AS_OF=cutoff)
    print('=' * 65)
    print('OKR BOOST SECOND PURCHASE - ACTUALIZACION AUTOMATICA')
    print('=' * 65)
    print('Fuente:', 'CSV ' + args.csv if args.csv else 'Trino / lake.clientes_trips_fact_full')
    print('Corte:', cutoff.isoformat())
    if args.csv:
        rows = from_csv(args.csv)
        aud_rows = list(csv.DictReader(open(args.audience_csv, newline='', encoding='utf-8-sig'))) if args.audience_csv else None
    else:
        rows, aud_rows = query_trino([kr_sql, aud_sql])
    print('Registros leidos:', len(rows))
    payloads = build(rows, cutoff)
    (ROOT / 'data').mkdir(exist_ok=True)
    for region, filename in [('Hispa', 'hispa.json'), ('Brasil', 'brasil.json')]:
        (ROOT / 'data' / filename).write_text(json.dumps(payloads[region], ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    summary = {'target_h2': TARGET, 'cutoff': cutoff.isoformat(),
               'ty': sum(v['ty'] for v in payloads.values()),
               'ly': sum(v['ly'] for v in payloads.values())}
    (ROOT / 'data/summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('OK: data/hispa.json, data/brasil.json, data/summary.json')
    for region in ('Hispa', 'Brasil'):
        p = payloads[region]
        print(f"{region}: TY={p['ty']:,} | LY={p['ly']:,} | fuera de base (no suma al KR) TY={p['out_of_base_ty']:,}")
    if aud_rows is not None:
        audience = build_audience(aud_rows, cutoff, payloads)
        (ROOT / 'data/audience.json').write_text(json.dumps(audience, ensure_ascii=False, indent=1), encoding='utf-8')
        print('OK: data/audience.json (%d filas)' % len(audience['rows']))
        for region in ('Hispa', 'Brasil'):
            for status in KR_CATEGORIES:
                el = sum(x['eligible'] for x in audience['rows'] if x['region'] == region and x['status'] == status)
                print(f"  base {region:6} {status:17} elegibles={el:>10,}")
        print('  Control recompradores base vs KR:')
        for c in audience['check_vs_kr']:
            print(f"    {c['region']:6} {c['status']:17} base={c['rebuyers_base']:>7,} KR={c['rebuyers_kr']:>7,} dif={c['diff_pct']}%")
    print('Corte:', cutoff, 'TY:', summary['ty'], 'LY:', summary['ly'], 'Target:', TARGET)


if __name__ == '__main__':
    main()

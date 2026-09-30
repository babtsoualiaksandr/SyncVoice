"""Radio station directory: import from the controllers' xlsx and text for the Gemini prompt."""
import re

import openpyxl

from .models import CITIES, RadioStation

# How controllers write stations in the report (seen in «Отчет записи 25.09.2026»).
# Used as the initial «в отчёте» value on import; editable in the app.
REPORT_NAMES = {
    'Радио РОКС': 'Рокс',
    'Русское радио (Минск)': 'Русское',
    'Русское радио (Могилев)': 'Русское',
    'Юмор FM': 'Юмор',
    'Душевное радио': 'Душевное',
    'Радиус FM': 'Радиус',
    'Радио Юнистар': 'Юнистар',
    'Радио Дельта': 'Дельта',
    'Новое Радио': 'Новое',
    'Радио ВИТЕБСК': 'Радио Витебск',
}


# Renames confirmed by the list itself (the note on Супер FM; same Minsk frequency).
KNOWN_ALIASES = {
    'Супер FM': ['Радио Би-Эй', 'Би-Эй'],
    'Радио Победа': ['Мелодии Века'],
    'Радио Релакс': ['Relax FM'],
}


def _clean(value) -> str:
    return re.sub(r'\s+', ' ', str(value)).strip() if value is not None else ''


def _frequency(value) -> str:
    """'95.0', 98.4, '107,4' -> '95.0', '98.4', '107.4'; blanks -> ''."""
    text = _clean(value).replace(',', '.')
    try:
        return f'{float(text):.1f}' if text else ''
    except ValueError:
        return ''


def _key(name: str) -> str:
    return re.sub(r'[^0-9a-zа-яё]', '', name.lower().replace('ё', 'е'))


def read_workbook(file) -> list[dict]:
    """Parse «Список РСТ с частотами.xlsx».

    The first sheet is the master list: № | название | частоты по городам
    (header row names the cities) | примечание. Per-city sheets
    (№ | English | Russian | частота) give other names; they are matched to
    the master list by Russian name, or by city + frequency (a station
    renamed on the same frequency, e.g. «Радио Би-Эй» -> «Супер FM»).
    """
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    sheets = wb.worksheets
    rows = list(sheets[0].iter_rows(values_only=True))
    header = [_clean(v) for v in rows[0]]
    city_cols = {i: c for i, c in enumerate(header) if c in CITIES}
    if not city_cols:
        raise ValueError('На первом листе не найдена строка с городами (Минск, Брест, …).')
    note_col = max(city_cols) + 1

    stations = []
    for row in rows[1:]:
        name = _clean(row[1]) if len(row) > 1 else ''
        if not name:
            continue
        freqs = {city: f for i, city in city_cols.items() if i < len(row) and (f := _frequency(row[i]))}
        stations.append({
            'name': name,
            'frequencies': freqs,
            'note': _clean(row[note_col]) if note_col < len(row) else '',
            'aliases': [],
        })

    by_name = {_key(s['name']): s for s in stations}
    for sheet in sheets[1:]:
        for row in sheet.iter_rows(values_only=True):
            if len(row) < 3 or not _clean(row[2]):
                continue
            english, russian = _clean(row[1]), _clean(row[2])
            station = by_name.get(_key(russian)) or _similar(by_name, _key(russian))
            if not station:
                continue
            for alias in (russian, english):
                if alias and _key(alias) != _key(station['name']) and alias not in station['aliases']:
                    station['aliases'].append(alias)
    for station in stations:
        for alias in KNOWN_ALIASES.get(station['name'], []):
            if alias not in station['aliases']:
                station['aliases'].append(alias)
    return stations


def _similar(by_name: dict, key: str):
    """The only station whose name contains `key` or is contained in it
    («Правда Радио» ~ «Правда Радио Гомель»). None if none or ambiguous."""
    if len(key) < 6:
        return None
    found = [s for k, s in by_name.items() if len(k) >= 6 and (k in key or key in k)]
    return found[0] if len(found) == 1 else None


def import_workbook(file) -> tuple[int, int]:
    """Create new stations and update frequencies/notes of known ones.

    Hand edits of «в отчёте», other names and «использовать» are kept;
    new aliases from the file are added to them. Returns (created, updated).
    """
    created = updated = 0
    for data in read_workbook(file):
        station = RadioStation.objects.filter(name=data['name']).first()
        if station is None:
            RadioStation.objects.create(
                name=data['name'],
                report_name=REPORT_NAMES.get(data['name'], ''),
                aliases='; '.join(data['aliases']),
                frequencies=data['frequencies'],
                note=data['note'],
            )
            created += 1
            continue
        known = station.alias_list()
        station.aliases = '; '.join(known + [a for a in data['aliases'] if a not in known])
        station.frequencies = data['frequencies']
        station.note = data['note']
        station.save()
        updated += 1
    return created, updated


def prompt_block() -> str:
    """The directory as text for the Gemini prompt."""
    lines = []
    for s in RadioStation.objects.filter(active=True):
        parts = [f'«{s.answer_name}»']
        names = [n for n in [s.name, *s.alias_list()] if n != s.answer_name]
        if names:
            parts.append('также: ' + ', '.join(names))
        if s.frequencies:
            parts.append('частоты: ' + ', '.join(
                f'{city} {s.frequencies[city]}' for city in CITIES if city in s.frequencies
            ))
        if s.note:
            parts.append(f'примечание: {s.note}')
        lines.append('- ' + '; '.join(parts))
    return '\n'.join(lines) or '(справочник пуст)'


def answer_names() -> list[str]:
    return list(dict.fromkeys(s.answer_name for s in RadioStation.objects.filter(active=True)))

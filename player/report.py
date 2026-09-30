"""Daily Excel report in the controllers' format («Отчет записи ДД.ММ.ГГГГ.xlsx»)."""
import io
import math
from datetime import date

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .models import AudioFile, CallAnalysis

# (header, width) for columns A..P, exactly as in the controllers' template.
COLUMNS = [
    (None, 4.2),
    ('Дата контроля', 10.7),
    ('№ телефона', 18.0),
    ('Дата опроса  Время', 21.0),
    ('Интервьюер: код (например, Ольга Инна Инна Инна 303 Марина) и Имя', 19.5),
    ('ID респондента', 5.8),
    ('Город проживания', 12.2),
    ('Город слушания', 10.5),
    ('Радиостанции / не слушал', 17.0),
    ('Ошибки ', 8.5),
    ('Комментарий к ошибкам', 29.3),
    ('Результат: брак/ошибки/ок', 16.2),
    ('Прим', 12.5),
    ('Продолжительность сек', 9.2),
    ('Контроль', 15.0),
    (None, 9.2),
]
LAST_FILTER_COLUMN = 'O'

# Styling copied from the template.
HEADER_HEIGHT = 68
HEADER_BORDERED = 'ABCDEFGHIJKLMN'  # «Контроль» and the unnamed last column have no frame
HEADER_LARGER = {'C': 12, 'E': 12}  # the rest of the header is 11 pt
NUMERIC_COLUMNS = 'CDEN'  # Arial, right-aligned, no wrap
LINE_HEIGHT = 16  # pt per text line in data rows
_thin = Side(style='thin')
_white = PatternFill(fill_type='solid', fgColor='FFFFFFFF')
# Highlighting controllers do by hand: an «ошибка» row is orange,
# a comment on an «ок» call (a recommendation) is green.
_error_row = PatternFill(fill_type='solid', fgColor='FFFFC000')
_recommendation = PatternFill(fill_type='solid', fgColor='FF00FF00')
# Gemini suggestions not yet confirmed by the controller: grey italic + a note.
AI_COLUMNS = {'G': 'city', 'H': 'listen_city', 'I': 'stations'}
_ai_font = Font(name='Calibri', size=11, italic=True, color='FF808080')
AI_NOTE = 'Подсказка ИИ по расшифровке, контролёр ещё не проверил.'


def _number_if_digits(value):
    """Phone / bare extension as a number, so Excel shows no «number stored as text» flag."""
    return int(value) if isinstance(value, str) and value.isdigit() else value


def report_filename(day: date) -> str:
    return f'Отчет записи {day:%d.%m.%Y}.xlsx'


def calls_for_day(day: date):
    return (
        AudioFile.objects.filter(call_started_at__date=day)
        .select_related('review', 'analysis')
        .order_by('call_started_at')
    )


def _lines(text, width: float) -> int:
    """Rough number of wrapped lines of `text` in a column of `width` characters."""
    if not text:
        return 1
    per_line = max(int(width * 1.1), 1)
    return sum(max(math.ceil(len(part) / per_line), 1) for part in str(text).split('\n'))


def build_report(day: date) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Лист1'
    widths = {}

    for i, (header, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=i, value=header)
        col = cell.column_letter
        widths[col] = width
        ws.column_dimensions[col].width = width
        cell.font = Font(name='Calibri', size=HEADER_LARGER.get(col, 11), bold=True)
        if col in HEADER_BORDERED:
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
        else:
            cell.alignment = Alignment(horizontal='center', wrap_text=True)
    ws.row_dimensions[1].height = HEADER_HEIGHT

    rows = []
    for audio in calls_for_day(day):
        review = getattr(audio, 'review', None)
        interviewer = review.interviewer if review and review.interviewer else audio.default_interviewer()
        rows.append((interviewer, audio, review))
    # Grouped by interviewer, then by call time — as controllers fill it in.
    rows.sort(key=lambda r: (r[0], r[1].call_started_at))

    for row_no, (interviewer, audio, review) in enumerate(rows, 2):
        started = timezone.localtime(audio.call_started_at).replace(tzinfo=None)
        values = {
            'B': review.review_date if review else None,
            'C': _number_if_digits(audio.phone),
            'D': started,
            'E': _number_if_digits(interviewer),
            'N': round(audio.duration) if audio.duration else None,
        }
        if review:
            values.update({
                'F': review.respondent_id,
                'G': review.city,
                'H': review.listen_city,
                'I': review.stations,
                'J': 'есть' if review.has_errors else '',
                'K': review.error_comment,
                'L': review.result,
                'M': review.note,
                'O': review.controller,
                'P': review.fixed,
            })
        # Fields the controller left empty are filled with the AI suggestion.
        suggestion = getattr(audio, 'analysis', None)
        ai_cells = set()
        if suggestion and suggestion.status == CallAnalysis.Status.DONE:
            for col, field in AI_COLUMNS.items():
                if not values.get(col) and getattr(suggestion, field):
                    values[col] = getattr(suggestion, field)
                    ai_cells.add(col)
        lines = 1
        for col in widths:
            cell = ws[f'{col}{row_no}']
            value = values.get(col)
            if value not in (None, ''):
                cell.value = value
            if col in NUMERIC_COLUMNS:
                cell.font = Font(name='Arial', size=11)
                cell.alignment = Alignment(horizontal='right')
            else:
                cell.font = Font(name='Calibri', size=11)
                cell.alignment = Alignment(wrap_text=True)
                cell.fill = _white
                lines = max(lines, _lines(value, widths[col]))
            if col in ai_cells:
                cell.font = _ai_font
                cell.comment = Comment(AI_NOTE, 'SyncVoice')
        if review and review.result == 'ошибка':
            for col in widths:
                ws[f'{col}{row_no}'].fill = _error_row
        elif review and review.error_comment:
            ws[f'K{row_no}'].fill = _recommendation
        ws[f'B{row_no}'].number_format = 'dd.mm.yyyy'
        ws[f'C{row_no}'].number_format = '0'
        ws[f'D{row_no}'].number_format = 'yyyy-mm-dd h:mm:ss'
        ws.row_dimensions[row_no].height = LINE_HEIGHT * lines

    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = f'A1:{LAST_FILTER_COLUMN}{max(len(rows) + 1, 2)}'

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

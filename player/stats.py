"""Interviewer names for call lists, sorting of a day's calls, per-interviewer statistics."""
from dataclasses import dataclass, field

from .models import CallReview, Interviewer

SORT_KEYS = ('time', 'interviewer', 'duration', 'status', 'result')
RESULT_ORDER = {'ок': 0, 'ошибка': 1, 'брак': 2}


def default_interviewers() -> dict[str, str]:
    """{extension: «Имя ext»}: the name last chosen in a review, else the first in Settings.

    Same rule as AudioFile.default_interviewer(), in two queries for a whole list.
    """
    names = {}
    for interviewer in Interviewer.objects.all():
        names.setdefault(interviewer.extension, str(interviewer))
    last_chosen = (
        CallReview.objects.exclude(interviewer='').order_by('updated_at')
        .values_list('audio__operator', 'interviewer')
    )
    for extension, name in last_chosen:
        names[extension] = name
    return names


def interviewer_label(audio, defaults: dict[str, str]) -> str:
    review = getattr(audio, 'review', None)
    if review and review.interviewer:
        return review.interviewer
    return defaults.get(audio.operator) or audio.operator or '—'


def label_calls(calls) -> list:
    """The calls as a list, each with .interviewer_label set."""
    defaults = default_interviewers()
    calls = list(calls)
    for audio in calls:
        audio.interviewer_label = interviewer_label(audio, defaults)
    return calls


def _review_state(audio) -> tuple:
    review = getattr(audio, 'review', None)
    if review and review.completed:
        return (0, RESULT_ORDER.get(review.result, 3))
    return (1, 0) if review else (2, 0)  # checked, draft, not started


def sort_calls(calls: list, sort: str) -> tuple[list, str]:
    """Sort labelled calls by «time», «interviewer», «duration», «status» or «result»;
    a leading «-» reverses. Unknown keys fall back to time. Returns (calls, used sort)."""
    key = sort.lstrip('-')
    if key not in SORT_KEYS:
        key, sort = 'time', 'time'
    by_time = lambda a: a.call_started_at  # noqa: E731
    keys = {
        'time': by_time,
        'interviewer': lambda a: (a.interviewer_label.lower(), a.call_started_at),
        'duration': lambda a: (a.duration or 0, a.call_started_at),
        'status': lambda a: (a.status, a.call_started_at),
        'result': lambda a: (_review_state(a), a.call_started_at),
    }
    return sorted(calls, key=keys[key], reverse=sort.startswith('-')), sort


@dataclass
class InterviewerStats:
    interviewer: str
    calls: int = 0
    reviewed: int = 0
    ok: int = 0
    errors: int = 0
    rejects: int = 0
    seconds: float = 0
    days: set = field(default_factory=set)

    @property
    def not_reviewed(self) -> int:
        return self.calls - self.reviewed

    @property
    def with_result(self) -> int:
        return self.ok + self.errors + self.rejects

    @property
    def error_share(self) -> float | None:
        """Share of «ошибка» and «брак» among reviewed calls that have a result, %."""
        return 100 * (self.errors + self.rejects) / self.with_result if self.with_result else None

    @property
    def avg_seconds(self) -> float | None:
        return self.seconds / self.calls if self.calls else None


def interviewer_stats(calls: list) -> tuple[list[InterviewerStats], InterviewerStats]:
    """Per-interviewer rows (by name) and a total row, for labelled calls."""
    rows: dict[str, InterviewerStats] = {}
    total = InterviewerStats('Всего')
    for audio in calls:
        row = rows.setdefault(audio.interviewer_label, InterviewerStats(audio.interviewer_label))
        for target in (row, total):
            target.calls += 1
            target.seconds += audio.duration or 0
            if audio.call_started_at:
                target.days.add(audio.call_started_at.date())
            review = getattr(audio, 'review', None)
            if review and review.completed:
                target.reviewed += 1
                target.ok += review.result == 'ок'
                target.errors += review.result == 'ошибка'
                target.rejects += review.result == 'брак'
    return sorted(rows.values(), key=lambda r: r.interviewer.lower()), total

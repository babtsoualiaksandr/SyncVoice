"""SyncVoice admin: every model, with status badges, filters and the AI re-run actions.

Branding and the section grouping live in admin_site.py; the colours in static/player/admin.css.
"""
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import analysis, compare, stats
from .models import (
    AppSettings, AudioFile, CallAnalysis, CallComparison, CallReview, GeminiPace, GeminiQuota, Interviewer, PbxSync,
    RadioStation, Segment,
)

BADGE_STATES = {  # status / result -> badge colour class
    'done': 'ok', 'ок': 'ok',
    'pending': 'wait', 'processing': 'wait', 'running': 'wait',
    'error': 'bad', 'ошибка': 'warn', 'брак': 'bad',
}


def badge(text, state):
    return format_html('<span class="sv-badge sv-{}">{}</span>', state, text) if text else '—'


def status_badge(obj):
    return badge(obj.get_status_display(), BADGE_STATES.get(obj.status, 'none'))


def call_link(audio):
    """The call in the admin, with its time and phone."""
    if not audio:
        return '—'
    when = timezone.localtime(audio.call_started_at).strftime('%d.%m %H:%M') if audio.call_started_at else ''
    url = reverse('admin:player_audiofile_change', args=[audio.pk])
    return format_html('<a href="{}">{} {}</a>', url, when, audio.phone_display or audio)


def player_link(audio):
    return format_html('<a class="sv-open" href="{}" target="_blank">▶ в плеере</a>', audio.get_absolute_url())


def verdict_badge(audio):
    verdict = stats.ai_verdict(audio)
    if not verdict:
        return '—'
    label = verdict['label'] + (f' ×{verdict["count"]}' if verdict['count'] > 1 else '')
    return format_html('<span class="sv-badge sv-verdict-{}" title="{}">{}</span>',
                       verdict['key'], verdict['title'], label)


def discrepancies_table(items):
    if not items:
        return 'Расхождений нет'
    rows = format_html_join(
        '', '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>',
        ((badge(d.get('severity', ''), {'ошибка': 'warn', 'проверить': 'wait'}.get(d.get('severity'), 'ok')),
          d.get('field', ''), d.get('survey_value', ''), d.get('call_value', ''), d.get('comment', ''))
         for d in items),
    )
    return format_html('<table class="sv-items"><thead><tr><th>Важность</th><th>Поле</th><th>Анкета</th>'
                       '<th>Разговор</th><th>Пояснение</th></tr></thead><tbody>{}</tbody></table>', rows)


class CreatedByAppMixin:
    """Rows SyncVoice creates itself (player, worker): edit or delete here, not add."""

    def has_add_permission(self, request, obj=None):
        return False


# ---------- inlines on the call page ----------

class ReviewInline(admin.StackedInline):
    model = CallReview
    extra = 0
    classes = ['collapse']
    fields = [('result', 'completed', 'has_errors'), ('interviewer', 'controller', 'review_date'),
              ('city', 'listen_city', 'stations'), 'error_comment', 'note', ('respondent_id', 'fixed')]


class AnalysisInline(admin.StackedInline):
    model = CallAnalysis
    extra = 0
    classes = ['collapse']
    fields = [('status', 'model_name', 'prompt_version'), ('city', 'listen_city', 'stations'), 'notes',
              ('input_tokens', 'output_tokens', 'updated_at'), 'error']
    readonly_fields = ['model_name', 'prompt_version', 'input_tokens', 'output_tokens', 'updated_at']


class ComparisonInline(admin.StackedInline):
    model = CallComparison
    extra = 0
    classes = ['collapse']
    fields = [('status', 'respondent_name', 'survey_id'), ('model_name', 'prompt_version', 'updated_at'),
              'summary', 'items', 'error']
    readonly_fields = ['items', 'model_name', 'prompt_version', 'updated_at']

    @admin.display(description='расхождения')
    def items(self, obj):
        return discrepancies_table(obj.discrepancies)


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    classes = ['collapse']
    fields = ['index', 'start', 'end', 'text']


# ---------- calls ----------

class ComparisonStatusFilter(admin.SimpleListFilter):
    title = 'сверка ИИ'
    parameter_name = 'check'

    def lookups(self, request, model_admin):
        return [*CallComparison.Status.choices, ('none', 'не запускалась')]

    def queryset(self, request, queryset):
        if self.value() == 'none':
            return queryset.filter(comparison=None)
        if self.value():
            return queryset.filter(comparison__status=self.value())
        return queryset


@admin.register(AudioFile)
class AudioFileAdmin(admin.ModelAdmin):
    list_display = ['started', 'phone_number', 'respondent', 'extension', 'length', 'subtitles',
                    'ai', 'control', 'open']
    list_filter = ['status', 'operator', 'review__completed', 'review__result', ComparisonStatusFilter, 'language']
    search_fields = ['phone', 'call_id', 'title', 'comparison__respondent_name', 'segments__text']
    date_hierarchy = 'call_started_at'
    list_select_related = ['review', 'analysis', 'comparison']
    list_per_page = 50
    inlines = [ReviewInline, AnalysisInline, ComparisonInline, SegmentInline]
    readonly_fields = ['created_at', 'open']
    fieldsets = [
        (None, {'fields': [('call_started_at', 'duration'), ('phone', 'operator'), ('status', 'language'),
                           'error', 'open']}),
        ('Файл', {'classes': ['collapse'], 'fields': ['title', 'file', 'call_id', 'sync', 'created_at']}),
    ]
    actions = ['requeue_transcription', 'requeue_ai']

    @admin.display(description='звонок', ordering='call_started_at')
    def started(self, obj):
        return timezone.localtime(obj.call_started_at).strftime('%d.%m.%Y %H:%M:%S') if obj.call_started_at else '—'

    @admin.display(description='телефон', ordering='phone')
    def phone_number(self, obj):
        return format_html('<span class="sv-nowrap">{}</span>', obj.phone_display or '—')

    @admin.display(description='внутр.', ordering='operator')
    def extension(self, obj):
        return obj.operator or '—'

    @admin.display(description='респондент', ordering='comparison__respondent_name')
    def respondent(self, obj):
        check = getattr(obj, 'comparison', None)
        return check.respondent_name if check and check.respondent_name else '—'

    @admin.display(description='длит.', ordering='duration')
    def length(self, obj):
        return f'{obj.duration:.0f} с' if obj.duration else '—'

    @admin.display(description='субтитры', ordering='status')
    def subtitles(self, obj):
        return status_badge(obj)

    @admin.display(description='ИИ')
    def ai(self, obj):
        return verdict_badge(obj)

    @admin.display(description='контроль', ordering='review__result')
    def control(self, obj):
        review = getattr(obj, 'review', None)
        if not review:
            return '—'
        if not review.completed:
            return badge('черновик', 'none')
        return badge(review.result or 'проверен', BADGE_STATES.get(review.result, 'ok'))

    @admin.display(description='')
    def open(self, obj):
        return player_link(obj) if obj.pk else ''

    @admin.action(description='Распознать заново')
    def requeue_transcription(self, request, queryset):
        count = queryset.update(status=AudioFile.Status.PENDING, error='')
        self.message_user(request, f'В очередь на распознавание: {count}.')

    @admin.action(description='Заново спросить ИИ (подсказка и сверка)')
    def requeue_ai(self, request, queryset):
        done = queryset.filter(status=AudioFile.Status.DONE)
        for audio in done:
            analysis.queue(audio)
            if compare.enabled():
                compare.queue(audio)
        self.message_user(request, f'В очередь ИИ: {done.count()} (нераспознанные пропущены).')


@admin.register(CallReview)
class CallReviewAdmin(CreatedByAppMixin, admin.ModelAdmin):
    list_display = ['call', 'interviewer', 'city', 'stations', 'result_badge', 'completed', 'controller',
                    'review_date']
    list_filter = ['completed', 'result', 'has_errors', 'review_date', 'controller']
    search_fields = ['audio__phone', 'interviewer', 'city', 'stations', 'error_comment', 'note']
    list_select_related = ['audio']
    date_hierarchy = 'review_date'
    autocomplete_fields = ['audio']
    readonly_fields = ['updated_at']

    @admin.display(description='звонок', ordering='audio__call_started_at')
    def call(self, obj):
        return call_link(obj.audio)

    @admin.display(description='результат', ordering='result')
    def result_badge(self, obj):
        return badge(obj.result, BADGE_STATES.get(obj.result, 'none'))


class AiAdmin(CreatedByAppMixin, admin.ModelAdmin):
    """Shared by the suggestion and the survey check."""
    list_select_related = ['audio']
    list_filter = ['status', 'model_name', 'prompt_version']
    autocomplete_fields = ['audio']
    actions = ['requeue']
    queue = None

    @admin.display(description='звонок', ordering='audio__call_started_at')
    def call(self, obj):
        return call_link(obj.audio)

    @admin.display(description='статус', ordering='status')
    def state(self, obj):
        return status_badge(obj)

    @admin.display(description='токены')
    def tokens(self, obj):
        return f'{obj.input_tokens} + {obj.output_tokens}' if obj.input_tokens else '—'

    @admin.action(description='Поставить в очередь заново')
    def requeue(self, request, queryset):
        for item in queryset.select_related('audio'):
            type(self).queue(item.audio)
        self.message_user(request, f'В очереди ИИ: {queryset.count()}. Их обработает запущенный SyncVoice.')


@admin.register(CallAnalysis)
class CallAnalysisAdmin(AiAdmin):
    list_display = ['call', 'state', 'city', 'listen_city', 'stations', 'model_name', 'prompt_version',
                    'tokens', 'updated_at']
    search_fields = ['audio__phone', 'city', 'stations', 'notes', 'error']
    readonly_fields = ['model_name', 'prompt_version', 'input_tokens', 'output_tokens', 'updated_at']
    queue = staticmethod(analysis.queue)


@admin.register(CallComparison)
class CallComparisonAdmin(AiAdmin):
    list_display = ['call', 'respondent_name', 'state', 'verdict', 'short_summary', 'model_name',
                    'prompt_version', 'tokens', 'updated_at']
    search_fields = ['audio__phone', 'respondent_name', 'summary', 'error']
    readonly_fields = ['items', 'model_name', 'prompt_version', 'input_tokens', 'output_tokens', 'updated_at']
    fields = ['audio', ('status', 'respondent_name', 'survey_id'), 'summary', 'items', 'error',
              ('model_name', 'prompt_version'), ('input_tokens', 'output_tokens', 'updated_at')]
    queue = staticmethod(compare.queue)

    @admin.display(description='вывод ИИ')
    def verdict(self, obj):
        return verdict_badge(obj.audio)

    @admin.display(description='итог')
    def short_summary(self, obj):
        return (obj.summary[:90] + '…') if len(obj.summary) > 90 else obj.summary or obj.error[:90]

    @admin.display(description='расхождения')
    def items(self, obj):
        return discrepancies_table(obj.discrepancies)


@admin.register(Segment)
class SegmentAdmin(CreatedByAppMixin, admin.ModelAdmin):
    list_display = ['call', 'index', 'start', 'end', 'text']
    search_fields = ['text', 'audio__phone']
    list_select_related = ['audio']
    autocomplete_fields = ['audio']

    @admin.display(description='звонок', ordering='audio__call_started_at')
    def call(self, obj):
        return call_link(obj.audio)


# ---------- directories ----------

@admin.register(Interviewer)
class InterviewerAdmin(admin.ModelAdmin):
    list_display = ['extension', 'name']
    list_display_links = ['extension']
    list_editable = ['name']
    search_fields = ['extension', 'name']


@admin.register(RadioStation)
class RadioStationAdmin(admin.ModelAdmin):
    list_display = ['name', 'report_name', 'aliases', 'freq', 'active']
    list_editable = ['active']
    list_filter = ['active']
    search_fields = ['name', 'report_name', 'aliases']

    @admin.display(description='частоты')
    def freq(self, obj):
        return ', '.join(f'{city} {f}' for city, f in (obj.frequencies or {}).items()) or '—'


# ---------- system ----------

@admin.register(PbxSync)
class PbxSyncAdmin(CreatedByAppMixin, admin.ModelAdmin):
    list_display = ['day', 'state', 'found', 'downloaded', 'skipped', 'failed', 'created_at', 'finished_at']
    list_filter = ['status']
    date_hierarchy = 'day'
    readonly_fields = ['found', 'downloaded', 'skipped', 'failed', 'created_at', 'finished_at']

    @admin.display(description='статус', ordering='status')
    def state(self, obj):
        return status_badge(obj)


@admin.register(GeminiQuota)
class GeminiQuotaAdmin(CreatedByAppMixin, admin.ModelAdmin):
    list_display = ['model', 'exhausted_until', 'active']
    actions = ['reset']

    @admin.display(description='сейчас', boolean=True)
    def active(self, obj):
        return obj.exhausted_until <= timezone.now()

    @admin.action(description='Снова пробовать эти модели (снять отметку о лимите)')
    def reset(self, request, queryset):
        count, _ = queryset.delete()
        self.message_user(request, f'Отметка снята: {count}.', messages.SUCCESS)


@admin.register(GeminiPace)
class GeminiPaceAdmin(CreatedByAppMixin, admin.ModelAdmin):
    """Read-only: when the next request to each model may go (see analysis.wait_for_slot)."""
    list_display = ['model', 'next_slot_at', 'per_minute']
    readonly_fields = ['model', 'next_slot_at']

    @admin.display(description='запросов в минуту')
    def per_minute(self, obj):
        rpm = analysis.requests_per_minute(obj.model)
        return f'{rpm:g}' if rpm else 'без ограничения'


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    """One row; the passwords are in the OS credential store, not here."""
    readonly_fields = ['worker_seen_at', 'models_in_use']
    fieldsets = [
        ('АТС', {'fields': ['pbx_url', 'pbx_username', 'pbx_verify_ssl', 'min_duration', 'max_duration']}),
        ('Контроль', {'fields': ['controller']}),
        ('CRM', {'fields': ['crm_url', 'crm_username', 'survey_url']}),
        ('ИИ (Gemini)', {'fields': ['ai_model', 'ai_compare_model', 'ai_fallback_models', 'models_in_use',
                                    'ai_rules_analysis', 'ai_rules_compare']}),
        ('Служебное', {'classes': ['collapse'], 'fields': ['worker_seen_at']}),
    ]

    @admin.display(description='сейчас используется')
    def models_in_use(self, obj):
        main, check = analysis.saved_models()
        return format_html('подсказки — <b>{}</b> ({}), сверка — <b>{}</b> ({}); запасные: {}',
                           main['model'], main['source'], check['model'], check['source'],
                           ', '.join(analysis.fallback_models()) or '—')

    def has_add_permission(self, request):
        return not AppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect('admin:player_appsettings_change', AppSettings.load().pk)


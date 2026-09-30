from django.contrib import admin

from .models import AudioFile, CallAnalysis, RadioStation, Segment


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    fields = ['index', 'start', 'end', 'text']


class AnalysisInline(admin.StackedInline):
    model = CallAnalysis
    extra = 0
    readonly_fields = ['model_name', 'input_tokens', 'output_tokens', 'updated_at']


@admin.register(AudioFile)
class AudioFileAdmin(admin.ModelAdmin):
    list_display = ['phone', 'operator', 'call_started_at', 'duration', 'status', 'title']
    list_filter = ['status', 'operator', 'language']
    search_fields = ['title', 'phone', 'call_id', 'segments__text']
    date_hierarchy = 'call_started_at'
    inlines = [AnalysisInline, SegmentInline]


@admin.register(RadioStation)
class RadioStationAdmin(admin.ModelAdmin):
    list_display = ['name', 'report_name', 'aliases', 'active']
    list_filter = ['active']
    search_fields = ['name', 'report_name', 'aliases']

from django.contrib import admin

from .models import AudioFile, Segment


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    fields = ['index', 'start', 'end', 'text']


@admin.register(AudioFile)
class AudioFileAdmin(admin.ModelAdmin):
    list_display = ['phone', 'operator', 'call_started_at', 'duration', 'status', 'title']
    list_filter = ['status', 'operator', 'language']
    search_fields = ['title', 'phone', 'call_id', 'segments__text']
    date_hierarchy = 'call_started_at'
    inlines = [SegmentInline]

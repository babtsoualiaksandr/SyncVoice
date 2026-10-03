"""SyncVoice's own admin: branding and the models grouped by what they are for."""
from django.contrib import admin
from django.contrib.admin.apps import AdminConfig

# Admin index sections: (title, model names in display order).
SECTIONS = [
    ('Звонки', ['AudioFile', 'CallReview', 'CallAnalysis', 'CallComparison', 'Segment']),
    ('Справочники', ['Interviewer', 'RadioStation']),
    ('Система', ['PbxSync', 'GeminiQuota', 'GeminiPace', 'AppSettings']),
]


class SyncVoiceAdminSite(admin.AdminSite):
    site_header = 'SyncVoice · администрирование'
    site_title = 'SyncVoice'
    index_title = 'Данные приложения'
    site_url = None  # «← В приложение» in the header instead of «Открыть сайт»

    def get_app_list(self, request, app_label=None):
        """The player's models split into SECTIONS; other apps (users, groups) after them."""
        apps = super().get_app_list(request, app_label)
        player = next((a for a in apps if a['app_label'] == 'player'), None)
        if not player:
            return apps
        models = {m['object_name']: m for m in player['models']}
        sections = []
        for title, names in SECTIONS:
            items = [models.pop(name) for name in names if name in models]
            if items:
                sections.append({**player, 'name': title, 'models': items})
        if models:  # a model not listed in SECTIONS still shows up
            sections.append({**player, 'name': 'Прочее', 'models': list(models.values())})
        return sections + [a for a in apps if a['app_label'] != 'player']


class SyncVoiceAdminConfig(AdminConfig):
    default_site = 'player.admin_site.SyncVoiceAdminSite'

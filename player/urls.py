from django.urls import path

from . import views

app_name = 'player'

urlpatterns = [
    path('', views.index, name='index'),
    path('upload/', views.upload, name='upload'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/test-connection/', views.test_connection, name='test_connection'),
    path('stations/', views.stations_view, name='stations'),
    path('stations/import/', views.stations_import, name='stations_import'),
    path('pbx/sync/', views.sync_start, name='sync_start'),
    path('status/', views.status, name='status'),
    path('report/<str:day>.xlsx', views.report, name='report'),
    path('audio/<int:pk>/', views.detail, name='detail'),
    path('audio/<int:pk>/review/', views.review_save, name='review_save'),
    path('audio/<int:pk>/analysis/', views.analysis_status, name='analysis'),
    path('audio/<int:pk>/stream/', views.stream, name='stream'),
    path('audio/<int:pk>/subtitles/', views.subtitles, name='subtitles'),
    path('audio/<int:pk>/subtitles.<str:fmt>', views.download_subtitles, name='download_subtitles'),
    path('audio/<int:pk>/retranscribe/', views.retranscribe, name='retranscribe'),
    path('audio/<int:pk>/delete/', views.delete, name='delete'),
]

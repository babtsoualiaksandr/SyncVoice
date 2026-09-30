from django.urls import path

from . import views

app_name = 'player'

urlpatterns = [
    path('', views.index, name='index'),
    path('audio/<int:pk>/', views.detail, name='detail'),
    path('audio/<int:pk>/stream/', views.stream, name='stream'),
    path('audio/<int:pk>/subtitles/', views.subtitles, name='subtitles'),
    path('audio/<int:pk>/subtitles.<str:fmt>', views.download_subtitles, name='download_subtitles'),
    path('audio/<int:pk>/retranscribe/', views.retranscribe, name='retranscribe'),
    path('audio/<int:pk>/delete/', views.delete, name='delete'),
]

from django.urls import path
from . import views

urlpatterns = [
    # Home page
    path('', views.home_view, name='home'),
    
    # Test view
    path('test/', views.test_view, name='test'),
    
    # Debug URLs
    path('debug/urls/', views.debug_urls, name='debug-urls'),
    
    # OAuth
    path('oauth/start/', views.oauth_start, name='oauth_start'),
    path('oauth/callback/', views.oauth_callback, name='oauth_callback'),
    
    # Auth status
    path('api/auth/status/', views.auth_status, name='auth-status'),
    
    # API endpoints
    path('unread-emails/', views.unread_emails_view, name='unread-emails'),
    path('drafts/', views.drafts_view, name='drafts'),
    path('generate/', views.generate_reply_view, name='generate-reply'),
    path('generate-tone-reply/', views.generate_tone_reply_view, name='generate-tone-reply'),
    path('save-draft/', views.save_draft_view, name='save-draft'),
    path('email/<str:message_id>/', views.email_detail_view, name='email-detail'),
    path('draft/<str:draft_id>/', views.draft_detail_view, name='draft-detail'),
    path('draft/<str:draft_id>/delete/', views.delete_draft_view, name='delete-draft'),
    path('draft/<str:draft_id>/update/', views.update_draft_view, name='update-draft'),
    path('mark-as-read/<str:message_id>/', views.mark_as_read_view, name='mark-as-read'),
    path('bulk-mark-as-read/', views.bulk_mark_as_read_view, name='bulk-mark-as-read'),
    path('schedule-meeting/', views.schedule_meeting_view, name='schedule-meeting'),
    path('check-calendar-permissions/', views.check_calendar_permissions_view, name='check-calendar-permissions'),
    path('voice-command/', views.voice_command_view, name='voice-command'),
    path('api/voice/commands/help', views.voice_command_help_view, name='voice-command-help'),
    
    # New API endpoints for Drafts, Important, and Settings
    path('generated-drafts/', views.generated_drafts_view, name='generated-drafts'),
    path('save-generated-draft/', views.save_generated_draft_view, name='save-generated-draft'),
    path('send-draft/<int:draft_id>/', views.send_draft_view, name='send-draft'),
    path('api/debug/drafts-db/', views.debug_drafts_db_view, name='debug-drafts-db'),  # NEW: Debug endpoint
    path('api/debug-drafts/', views.debug_drafts_view, name='debug-drafts'),  # Existing debug view
    path('delete-generated-draft/<int:draft_id>/', views.delete_generated_draft_view, name='delete-generated-draft'),
    path('toggle-important/<str:message_id>/', views.toggle_important_view, name='toggle-important'),
    path('important-emails/', views.important_emails_view, name='important-emails'),
    path('api/user-settings/', views.user_settings_view, name='user-settings'),
    
    # Settings page
    path('settings/', views.settings_view, name='settings'),
    
    # Account management
    path('logout/', views.logout_view, name='logout'),
    path('force-reauth/', views.force_reauth_view, name='force_reauth'),
    
    path('api/test/gmail-drafts/', views.test_gmail_drafts_view, name='test-gmail-drafts'),
    
    path('draft/<str:draft_id>/edit/', views.edit_gmail_draft_view, name='edit-gmail-draft'),
    

    # Archive email endpoints
    path('archive-email/<str:message_id>/', views.archive_email_view, name='archive_email'),
    path('bulk-archive-emails/', views.bulk_archive_emails_view, name='bulk_archive_emails'),
    
    # Your other URL patterns...
]

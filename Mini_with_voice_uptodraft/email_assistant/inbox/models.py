from django.db import models
from django.contrib.auth.models import User

class GmailCredentials(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    token = models.TextField()
    refresh_token = models.TextField()
    token_uri = models.CharField(max_length=255)
    client_id = models.CharField(max_length=255)
    client_secret = models.CharField(max_length=255)
    scopes = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Gmail Credential"
        verbose_name_plural = "Gmail Credentials"
        
    def __str__(self):
        return f"{self.user.username if self.user else 'Anonymous'} - {self.client_id}"

class GeneratedDraft(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    original_email_id = models.CharField(max_length=255)  # ID of the original email
    subject = models.CharField(max_length=255)
    recipient = models.CharField(max_length=255)
    reply_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_sent = models.BooleanField(default=False)
    
    def __str__(self):
        return f"Draft for {self.subject} to {self.recipient}"

class ImportantEmail(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message_id = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('user', 'message_id')
    
    def __str__(self):
        return f"Important email {self.message_id} for {self.user.username}"

class UserSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    reply_tone = models.CharField(max_length=20, choices=[
        ('professional', 'Professional'),
        ('friendly', 'Friendly'),
        ('casual', 'Casual'),
    ], default='professional')
    auto_reply_enabled = models.BooleanField(default=True)
    refresh_interval = models.IntegerField(default=5, help_text="Refresh interval in minutes")
    theme = models.CharField(max_length=10, choices=[
        ('light', 'Light'),
        ('dark', 'Dark'),
    ], default='light')
    
    def __str__(self):
        return f"Settings for {self.user.username}"
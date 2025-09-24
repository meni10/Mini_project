# serializers.py
from rest_framework import serializers

class EmailSerializer(serializers.Serializer):
    id = serializers.CharField()
    threadId = serializers.CharField(required=False, allow_blank=True)
    snippet = serializers.CharField(allow_blank=True)
    subject = serializers.CharField(allow_blank=True)
    from_field = serializers.CharField(source="from", allow_blank=True)
    to = serializers.CharField(allow_blank=True, required=False)
    date = serializers.CharField(allow_blank=True)
    body_text = serializers.CharField(allow_blank=True, required=False, default='')
    is_important = serializers.BooleanField(required=False, default=False)
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['subject'] = representation.get('subject') or '(no subject)'
        representation['from_field'] = representation.get('from_field') or '(unknown sender)'
        representation['body_text'] = representation.get('body_text') or ''
        return representation

class DraftSerializer(serializers.Serializer):
    """Separate serializer for drafts that doesn't require body_text"""
    id = serializers.CharField()
    subject = serializers.CharField(allow_blank=True)
    from_field = serializers.CharField(source="from", allow_blank=True)
    date = serializers.CharField(allow_blank=True)
    snippet = serializers.CharField(allow_blank=True)
    is_draft = serializers.BooleanField(default=True)
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        # Ensure subject and from_field have fallback values
        representation['subject'] = representation.get('subject', '(no subject)')
        representation['from_field'] = representation.get('from_field', 'Unknown')
        return representation
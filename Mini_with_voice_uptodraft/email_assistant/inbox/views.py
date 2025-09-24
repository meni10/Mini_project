import os
import json
import logging
import base64
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth import login
from django.urls import resolve, reverse
from django.contrib import messages
from django.core.cache import cache
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.core.paginator import Paginator, EmptyPage
from .serializers import EmailSerializer
from .services import gemini
from .services.gmail import (
    build_flow, get_gmail_service, create_gmail_draft,
    _save_creds_to_db, _load_creds_from_db, fetch_unread, mark_as_read,
    fetch_drafts, get_draft_details, delete_draft, update_draft
)
from .models import GeneratedDraft, ImportantEmail, UserSettings

# Logging
logger = logging.getLogger(__name__)


########################################
# Helper Functions
########################################
def credentials_to_dict(credentials):
    """Convert credentials object to dictionary"""
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes,
    }

def is_authenticated(user=None):
    """Check if user has valid Gmail credentials"""
    try:
        # Try to get service - if it works, we're authenticated
        service = get_gmail_service(user=user)
        return service is not None
    except:
        return False

########################################
# Test View
########################################
def test_view(request):
    """Simple test view to check if the basic setup works"""
    return HttpResponse("Test view works!")

########################################
# Debug URLs
########################################
def debug_urls(request):
    path_info = request.path_info
    try:
        resolver_match = resolve(path_info)
        return JsonResponse({
            "status": "matched",
            "view_name": resolver_match.view_name,
            "app_name": resolver_match.app_name,
            "namespace": resolver_match.namespace,
            "url_name": resolver_match.url_name,
            "function": str(resolver_match.func)
        })
    except Exception as e:
        return JsonResponse({
            "status": "not matched",
            "error": str(e),
            "path": path_info
        })

########################################
# Home page
########################################
def home_view(request):
    try:
        # Check if we have a session variable indicating authentication
        gmail_authenticated = request.session.get('gmail_authenticated', False)
        
        # Try to get the service with better error handling
        service = None
        authed = False
        try:
            service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
            authed = service is not None
        except Exception as e:
            logger.error(f"Error getting Gmail service in home_view: {str(e)}", exc_info=True)
            authed = False
        
        # If we have a session variable but no service, clear the session variable
        if gmail_authenticated and not authed:
            request.session['gmail_authenticated'] = False
        
        return render(request, "inbox/home.html", {
            "authed": authed,
            "gmail_authenticated": gmail_authenticated,
            "user": request.user if request.user.is_authenticated else None
        })
    except Exception as e:
        logger.error(f"Error in home_view: {str(e)}", exc_info=True)
        return HttpResponse(f"Error loading home page: {str(e)}", status=500)

########################################
# OAuth status
########################################
def auth_status(request):
    service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
    
    # Test the service by trying to fetch the user's profile
    profile = None
    if service:
        try:
            profile = service.users().getProfile(userId="me").execute()
            logger.info(f"Successfully fetched Gmail profile: {profile.get('emailAddress')}")
        except Exception as e:
            logger.error(f"Error fetching Gmail profile: {str(e)}", exc_info=True)
            service = None
    
    return JsonResponse({
        "authenticated": service is not None,
        "django_authenticated": request.user.is_authenticated,
        "email": request.user.email if request.user.is_authenticated else None,
        "username": request.user.username if request.user.is_authenticated else None,
        "gmail_profile": profile,
    })
    
########################################
# OAuth start (Updated to force account selection)
########################################

def oauth_start(request):
    try:
        # Clear any existing OAuth state to avoid conflicts
        if 'oauth_state' in request.session:
            del request.session['oauth_state']
        
        redirect_uri = request.build_absolute_uri("/oauth/callback/")
        logger.info(f"OAuth start with redirect_uri: {redirect_uri}")
        
        flow = build_flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent select_account",
        )
        request.session["oauth_state"] = state
        logger.info(f"OAuth redirecting to: {auth_url}")
        return redirect(auth_url)
        
    except Exception as e:
        logger.error(f"OAuth start error: {str(e)}", exc_info=True)
        return HttpResponseBadRequest(f"OAuth initialization failed: {str(e)}")

########################################
# OAuth callback (FIXED with Django authentication)
########################################
def oauth_callback(request):
    try:
        expected_state = request.session.get("oauth_state")
        returned_state = request.GET.get("state")
        
        if not expected_state or expected_state != returned_state:
            logger.error(f"State mismatch. Expected: {expected_state}, Got: {returned_state}")
            return HttpResponseBadRequest("Invalid OAuth state. Please try again.")
            
        redirect_uri = request.build_absolute_uri("/oauth/callback/")
        logger.info(f"OAuth callback with redirect_uri: {redirect_uri}")
        
        flow = build_flow(redirect_uri)
        
        # Handle scope changes by updating the flow's scopes with the returned scopes
        returned_scopes = request.GET.get("scope", "").split()
        if returned_scopes:
            flow.scopes = returned_scopes
        
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        creds = flow.credentials
        
        # Get user info from Google
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        
        id_info = id_token.verify_oauth2_token(
            creds.id_token,
            request=google_requests.Request(),
            audience=creds.client_id
        )
        
        email = id_info.get("email")
        name = id_info.get("name", "")
        
        # Get or create Django user
        try:
            user = User.objects.get(email=email)
            # Update user info if needed
            if not user.first_name and name:
                name_parts = name.split()
                if len(name_parts) >= 1:
                    user.first_name = name_parts[0]
                if len(name_parts) >= 2:
                    user.last_name = " ".join(name_parts[1:])
                user.save()
            logger.info(f"Existing user logged in: {user.username}")
        except User.DoesNotExist:
            # Create new user
            username = email.split('@')[0]
            # Ensure username is unique
            base_username = username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{counter}"
                counter += 1
                
            user = User.objects.create_user(
                username=username,
                email=email,
                first_name=name.split()[0] if name else "",
                last_name=" ".join(name.split()[1:]) if name and " " in name else ""
            )
            user.save()
            logger.info(f"New user created: {user.username}")
        
        # FIXED: Specify the authentication backend when logging in
        from django.contrib.auth import login
        from django.contrib.auth.backends import ModelBackend
        
        # Log in the user with explicit backend
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        logger.info(f"User {user.username} logged in successfully")
        
        # Ensure we have a session
        if not request.session.session_key:
            request.session.create()
        
        # Save credentials to database with proper user handling
        _save_creds_to_db(creds, user=user)
        
        # Log successful save
        logger.info(f"OAuth successful. Credentials saved to database for user: {user.username}")
        
        # Clear OAuth state
        request.session.pop("oauth_state", None)
        
        # Set session variable to indicate authentication
        request.session['gmail_authenticated'] = True
        
        return redirect("home")
        
    except Exception as e:
        logger.error(f"OAuth callback error: {str(e)}", exc_info=True)
        error_message = f"OAuth authentication failed: {str(e)}"
        
        # Check for common OAuth errors
        if "invalid_grant" in str(e):
            error_message += ". This may be due to an expired or invalid authorization code. Please try authenticating again."
        elif "redirect_uri_mismatch" in str(e):
            error_message += ". Redirect URI mismatch. Check your Google Cloud Console configuration."
        elif "access_denied" in str(e):
            error_message = "Access denied. This application is in testing mode and your email is not registered as a test user. Please contact the administrator to add your email to the test users list."
        
        return HttpResponseBadRequest(error_message)

########################################
# API: unread emails with pagination
########################################
@api_view(['GET'])
def unread_emails_view(request):
    page_number = request.GET.get('page', 1)
    per_page = request.GET.get('per_page', 10)  # Allow configurable items per page
    
    try:
        page_number = int(page_number)
        if page_number < 1:
            page_number = 1
    except ValueError:
        page_number = 1
        
    try:
        per_page = int(per_page)
        if per_page < 1:
            per_page = 10
        if per_page > 50:  # Limit max items per page
            per_page = 50
    except ValueError:
        per_page = 10
        
    try:
        user = request.user if request.user.is_authenticated else None
        service = get_gmail_service(user=user)
        
        if not service:
            logger.warning("Failed to get Gmail service - user not authenticated or no valid credentials")
            return Response({
                'ok': False, 
                'error': 'Authentication required. Please connect your Gmail account.',
                'auth_required': True
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        logger.info(f"Fetching unread emails for user: {user if user else 'anonymous'}")
        
        # Use the fetch_unread from services.gmail
        emails = fetch_unread(service, max_results=100)  # Fetch more emails for pagination
        
        if not emails:
            logger.warning("No unread emails found")
            return Response({'ok': True, 'emails': [], 'total_pages': 0, 'current_page': 1})
        
        # Add important status to each email
        if user and user.is_authenticated:
            important_message_ids = ImportantEmail.objects.filter(user=user).values_list('message_id', flat=True)
            for email in emails:
                email['is_important'] = email['id'] in important_message_ids
        
        paginator = Paginator(emails, per_page)
        try:
            page_obj = paginator.page(page_number)
        except EmptyPage:
            logger.warning(f"Requested page {page_number} out of range. Returning last page.")
            page_obj = paginator.page(paginator.num_pages)
        
        serializer = EmailSerializer(page_obj.object_list, many=True, context={'user': user})
        return Response({
            'ok': True,
            'emails': serializer.data,
            'total_pages': paginator.num_pages,
            'current_page': page_obj.number,
            'per_page': per_page,
            'total_emails': len(emails),
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        })
    except Exception as e:
        logger.error(f"Error in unread_emails_view: {str(e)}", exc_info=True)
        error_msg = "Failed to fetch emails"
        
        # Provide more specific error messages
        if "rateLimitExceeded" in str(e):
            error_msg = "Gmail API rate limit exceeded. Please try again later."
        elif "invalid_grant" in str(e):
            error_msg = "Authentication expired. Please reconnect your Gmail account."
        
        return Response({
            'ok': False, 
            'error': error_msg,
            'auth_required': "invalid_grant" in str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: drafts with pagination
########################################

@api_view(['GET'])
def drafts_view(request):
    page_number = request.GET.get('page', 1)
    per_page = request.GET.get('per_page', 10)
    refresh = request.GET.get('refresh', 'false').lower() == 'true'  # New refresh parameter
    
    try:
        page_number = int(page_number)
        if page_number < 1:
            page_number = 1
    except ValueError:
        page_number = 1
        
    try:
        per_page = int(per_page)
        if per_page < 1:
            per_page = 10
        if per_page > 50:
            per_page = 50
    except ValueError:
        per_page = 10
        
    try:
        user = request.user if request.user.is_authenticated else None
        service = get_gmail_service(user=user)
        
        if not service:
            return Response({
                'ok': False, 
                'error': 'Authentication required. Please connect your Gmail account.',
                'auth_required': True
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        # Get authenticated email for verification
        try:
            profile = service.users().getProfile(userId='me').execute()
            authenticated_email = profile.get('emailAddress')
            logger.info(f"Fetching drafts for authenticated Gmail account: {authenticated_email}")
        except Exception as e:
            logger.error(f"Error getting Gmail profile: {str(e)}")
            authenticated_email = "Unknown"
        
        # Clear cache if refresh requested
        if refresh:
            cache_key = f"gmail_drafts_{per_page}"
            cache.delete(cache_key)
            logger.info(f"Cleared drafts cache on refresh request")
        
        # Fetch drafts from Gmail
        drafts = fetch_drafts(service, max_results=100)
        
        if not drafts:
            logger.warning("No drafts returned from fetch_drafts")
            return Response({
                'ok': True, 
                'drafts': [], 
                'total_pages': 0, 
                'current_page': 1,
                'authenticated_email': authenticated_email,
                'debug_info': "No drafts found. Check logs for details."
            })
        
        # Use DraftSerializer for drafts
        from .serializers import DraftSerializer
        paginator = Paginator(drafts, per_page)
        try:
            page_obj = paginator.page(page_number)
        except EmptyPage:
            logger.warning(f"Requested page {page_number} out of range. Returning last page.")
            page_obj = paginator.page(paginator.num_pages)
        
        serializer = DraftSerializer(page_obj.object_list, many=True)
        return Response({
            'ok': True,
            'drafts': serializer.data,
            'total_pages': paginator.num_pages,
            'current_page': page_obj.number,
            'per_page': per_page,
            'total_drafts': len(drafts),
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
            'authenticated_email': authenticated_email
        })
    except Exception as e:
        logger.error(f"Error in drafts_view: {str(e)}", exc_info=True)
        error_msg = "Failed to fetch drafts"
        
        if "rateLimitExceeded" in str(e):
            error_msg = "Gmail API rate limit exceeded. Please try again later."
        elif "invalid_grant" in str(e):
            error_msg = "Authentication expired. Please reconnect your Gmail account."
        
        return Response({
            'ok': False, 
            'error': error_msg,
            'auth_required': "invalid_grant" in str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
             
@api_view(['GET'])
def debug_drafts_view(request):
    """Debug view to test draft fetching"""
    try:
        user = request.user if request.user.is_authenticated else None
        service = get_gmail_service(user=user)
        
        if not service:
            return Response({'error': 'No Gmail service available'}, status=400)
        
        # Test 1: Get profile
        try:
            profile = service.users().getProfile(userId='me').execute()
            profile_info = {
                'emailAddress': profile.get('emailAddress'),
                'historyId': profile.get('historyId'),
                'messagesTotal': profile.get('messagesTotal'),
                'threadsTotal': profile.get('threadsTotal')
            }
        except Exception as e:
            profile_info = {'error': str(e)}
        
        # Test 2: List drafts
        try:
            drafts_list = service.users().drafts().list(userId='me', maxResults=5).execute()
            draft_ids = [draft['id'] for draft in drafts_list.get('drafts', [])]
        except Exception as e:
            drafts_list = {'error': str(e)}
            draft_ids = []
        
        # Test 3: Get first draft details
        draft_details = None
        if draft_ids:
            try:
                draft_details = service.users().drafts().get(userId='me', id=draft_ids[0]).execute()
            except Exception as e:
                draft_details = {'error': str(e)}
        
        return Response({
            'profile': profile_info,
            'drafts_list': drafts_list,
            'draft_ids': draft_ids,
            'draft_details': draft_details
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

########################################
# API: generated drafts (FIXED)
########################################
@api_view(['GET'])
def generated_drafts_view(request):
    """Get all generated drafts for the current user"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        drafts = GeneratedDraft.objects.filter(user=request.user, is_sent=False).order_by('-created_at')
        
        draft_data = []
        for draft in drafts:
            draft_data.append({
                'id': draft.id,
                'subject': draft.subject,
                'recipient': draft.recipient,
                'reply_text': draft.reply_text,
                'created_at': draft.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'original_email_id': draft.original_email_id
            })
            
        logger.info(f"Returning {len(draft_data)} generated drafts for user {request.user.username}")
        return Response({'ok': True, 'drafts': draft_data})
    except Exception as e:
        logger.error(f"Error fetching generated drafts: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: Debug generated drafts database (NEW)
########################################
@api_view(['GET'])
def debug_drafts_db_view(request):
    """Debug view to check GeneratedDraft database entries"""
    try:
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=401)
            
        # Get all drafts for this user
        drafts = GeneratedDraft.objects.filter(user=request.user)
        
        draft_info = []
        for draft in drafts:
            draft_info.append({
                'id': draft.id,
                'subject': draft.subject,
                'recipient': draft.recipient,
                'reply_text_preview': draft.reply_text[:100] + '...' if len(draft.reply_text) > 100 else draft.reply_text,
                'created_at': draft.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'is_sent': draft.is_sent,
                'original_email_id': draft.original_email_id
            })
        
        return Response({
            'total_drafts': drafts.count(),
            'drafts': draft_info,
            'user': request.user.username,
            'user_email': request.user.email
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

########################################
# API: save generated draft (FIXED)
########################################
@api_view(['POST'])
def save_generated_draft_view(request):
    """Save a generated reply as a draft"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        payload = json.loads(request.body.decode("utf-8"))
        original_email_id = payload.get("original_email_id", "")
        subject = payload.get("subject", "")
        recipient = payload.get("recipient", "")
        reply_text = payload.get("reply_text", "")
        
        logger.info(f"Saving draft for user {request.user.username}:")
        logger.info(f"  - Subject: {subject}")
        logger.info(f"  - Recipient: {recipient}")
        logger.info(f"  - Original Email ID: {original_email_id}")
        logger.info(f"  - Reply Text Length: {len(reply_text)}")
        
        if not all([original_email_id, subject, recipient, reply_text]):
            logger.error("Missing required fields for draft")
            return HttpResponseBadRequest("Missing required fields")
            
        draft = GeneratedDraft.objects.create(
            user=request.user,
            original_email_id=original_email_id,
            subject=subject,
            recipient=recipient,
            reply_text=reply_text
        )
        
        logger.info(f"Draft saved successfully with ID: {draft.id}")
        return JsonResponse({"ok": True, "draft_id": draft.id})
    except Exception as e:
        logger.error(f"Error saving generated draft: {str(e)}", exc_info=True)
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

########################################
# API: send draft
########################################
@api_view(['POST'])
def send_draft_view(request, draft_id):
    """Send a generated draft"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        draft = GeneratedDraft.objects.get(id=draft_id, user=request.user)
        
        # Get Gmail service
        service = get_gmail_service(user=request.user)
        if not service:
            return Response({'ok': False, 'error': 'Gmail not connected'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Create and send the message
        from .services.gmail import create_message
        message = create_message(draft.recipient, draft.subject, draft.reply_text)
        sent_message = service.users().messages().send(userId='me', body=message).execute()
        
        # Mark draft as sent
        draft.is_sent = True
        draft.save()
        
        return JsonResponse({"ok": True, "message_id": sent_message['id']})
    except GeneratedDraft.DoesNotExist:
        return Response({'ok': False, 'error': 'Draft not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error sending draft: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: delete generated draft
########################################
@api_view(['DELETE'])
def delete_generated_draft_view(request, draft_id):
    """Delete a generated draft"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        draft = GeneratedDraft.objects.get(id=draft_id, user=request.user)
        draft.delete()
        
        return Response({'ok': True})
    except GeneratedDraft.DoesNotExist:
        return Response({'ok': False, 'error': 'Draft not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error deleting generated draft: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: toggle important
########################################
@api_view(['POST'])
def toggle_important_view(request, message_id):
    """Toggle important status for an email"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        important_email, created = ImportantEmail.objects.get_or_create(
            user=request.user,
            message_id=message_id
        )
        
        if not created:
            # If it already exists, remove it (unmark as important)
            important_email.delete()
            is_important = False
        else:
            is_important = True
            
        return Response({'ok': True, 'is_important': is_important})
    except Exception as e:
        logger.error(f"Error toggling important status: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: important emails
########################################
@api_view(['GET'])
def important_emails_view(request):
    """Get all important emails for the current user"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        # Get all important message IDs for this user
        important_message_ids = ImportantEmail.objects.filter(user=request.user).values_list('message_id', flat=True)
        
        if not important_message_ids:
            return Response({'ok': True, 'emails': []})
            
        # Get Gmail service
        service = get_gmail_service(user=request.user)
        if not service:
            return Response({'ok': False, 'error': 'Gmail not connected'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Fetch email details for each important message
        emails = []
        for message_id in important_message_ids:
            try:
                from .services.gmail import get_email_details
                email_details = get_email_details(service, message_id)
                if email_details:
                    # Mark as important
                    email_details['is_important'] = True
                    emails.append(email_details)
            except Exception as e:
                logger.error(f"Error fetching email {message_id}: {str(e)}")
                continue
        
        # Sort by date
        emails.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        serializer = EmailSerializer(emails, many=True, context={'user': request.user})
        return Response({'ok': True, 'emails': serializer.data})
    except Exception as e:
        logger.error(f"Error fetching important emails: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: user settings
########################################
@api_view(['GET', 'POST'])
def user_settings_view(request):
    """Get or update user settings"""
    try:
        if not request.user.is_authenticated:
            return Response({'ok': False, 'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        if request.method == 'GET':
            # Get user settings
            user_settings, created = UserSettings.objects.get_or_create(user=request.user)
            
            return Response({
                'ok': True,
                'settings': {
                    'reply_tone': user_settings.reply_tone,
                    'auto_reply_enabled': user_settings.auto_reply_enabled,
                    'refresh_interval': user_settings.refresh_interval,
                    'theme': user_settings.theme
                }
            })
        else:
            # Update user settings
            payload = json.loads(request.body.decode("utf-8"))
            reply_tone = payload.get("reply_tone", "professional")
            auto_reply_enabled = payload.get("auto_reply_enabled", True)
            refresh_interval = payload.get("refresh_interval", 5)
            theme = payload.get("theme", "light")
            
            # Validate values
            if reply_tone not in ['professional', 'friendly', 'casual']:
                return Response({'ok': False, 'error': 'Invalid reply tone'}, status=status.HTTP_400_BAD_REQUEST)
            
            if not isinstance(refresh_interval, int) or refresh_interval < 1 or refresh_interval > 60:
                return Response({'ok': False, 'error': 'Refresh interval must be between 1 and 60 minutes'}, status=status.HTTP_400_BAD_REQUEST)
            
            if theme not in ['light', 'dark']:
                return Response({'ok': False, 'error': 'Invalid theme'}, status=status.HTTP_400_BAD_REQUEST)
            
            # Update settings
            user_settings, created = UserSettings.objects.get_or_create(user=request.user)
            user_settings.reply_tone = reply_tone
            user_settings.auto_reply_enabled = auto_reply_enabled
            user_settings.refresh_interval = refresh_interval
            user_settings.theme = theme
            user_settings.save()
            
            # Update session for theme
            request.session['theme'] = theme
            
            return Response({
                'ok': True,
                'settings': {
                    'reply_tone': user_settings.reply_tone,
                    'auto_reply_enabled': user_settings.auto_reply_enabled,
                    'refresh_interval': user_settings.refresh_interval,
                    'theme': user_settings.theme
                }
            })
    except Exception as e:
        logger.error(f"Error in user_settings_view: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# Settings page
########################################
@login_required
def settings_view(request):
    try:
        # Get or create user settings
        user_settings, created = UserSettings.objects.get_or_create(user=request.user)
        
        if request.method == 'POST':
            # Update settings
            user_settings.reply_tone = request.POST.get('reply_tone', 'professional')
            user_settings.auto_reply_enabled = request.POST.get('auto_reply_enabled', 'off') == 'on'
            user_settings.refresh_interval = int(request.POST.get('refresh_interval', 5))
            user_settings.theme = request.POST.get('theme', 'light')
            user_settings.save()
            
            # Update session for theme
            request.session['theme'] = user_settings.theme
            
            messages.success(request, "Settings saved successfully!")
            return redirect('settings')
        
        # Check if user has Gmail connected
        service = get_gmail_service(user=request.user)
        gmail_connected = service is not None
        
        return render(request, "inbox/settings.html", {
            'gmail_connected': gmail_connected,
            'settings': user_settings
        })
    except Exception as e:
        logger.error(f"Error in settings view: {str(e)}", exc_info=True)
        messages.error(request, f"Error loading settings: {str(e)}")
        return redirect('home')

########################################
# API: generate reply with Gemini
########################################
@csrf_exempt
@require_POST
def generate_reply_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        email_text = payload.get("email_text", "")
        message_id = payload.get("message_id", "")
        subject = payload.get("subject", "")
        from_email = payload.get("from_email", "")
        
        if not email_text:
            return HttpResponseBadRequest("email_text is required")
        
        # Get user settings for reply tone
        reply_tone = 'professional'  # Default
        if request.user.is_authenticated:
            try:
                user_settings = UserSettings.objects.get(user=request.user)
                reply_tone = user_settings.reply_tone
            except UserSettings.DoesNotExist:
                pass
        
        summary = gemini.summarize_email(email_text)
        
        # Use the existing generate_reply function with tone-specific prompt
        tone_prompt = f"""
        Generate a reply to the following email in a {reply_tone} tone.
        The tone should be {reply_tone} throughout the entire response.
        
        Email Summary: {summary}
        
        Original Email: {email_text}
        
        Reply:
        """
        
        draft = gemini.generate_reply(tone_prompt)
        
        # Save as draft if user is authenticated
        if request.user.is_authenticated and message_id and subject and from_email:
            GeneratedDraft.objects.create(
                user=request.user,
                original_email_id=message_id,
                subject=f"Re: {subject}",
                recipient=from_email,
                reply_text=draft
            )
        
        return JsonResponse({"ok": True, "summary": summary, "draft_reply": draft})
    except Exception as e:
        logger.error(f"Generate reply error: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)
    
########################################
# API: generate reply with tone using Gemini
########################################
@csrf_exempt
@require_POST
def generate_tone_reply_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        email_text = payload.get("email_text", "")
        tone = payload.get("tone", "professional")  # Default to professional if not provided
        
        if not email_text:
            return HttpResponseBadRequest("email_text is required")
        
        summary = gemini.summarize_email(email_text)
        
        # Create a tone-specific prompt
        tone_prompt = f"""
        Generate a reply to the following email in a {tone} tone.
        The tone should be {tone} throughout the entire response.
        
        Email Summary: {summary}
        
        Original Email: {email_text}
        
        Reply:
        """
        
        # Use the existing gemini service with our custom prompt
        draft = gemini.generate_reply(tone_prompt)
        
        return JsonResponse({"ok": True, "summary": summary, "draft_reply": draft})
    except Exception as e:
        logger.error(f"Generate tone reply error: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

########################################
# API: save draft to Gmail (FIXED)
########################################
@csrf_exempt
@require_POST
def save_draft_view(request):
    service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
    if not service:
        return JsonResponse({"ok": False, "error": "Not authorized. Connect Gmail first."}, status=401)
    
    try:
        payload = json.loads(request.body.decode("utf-8"))
        to_email = payload.get("to")
        subject = payload.get("subject")
        body = payload.get("body")
        
        if not all([to_email, subject, body]):
            logger.error(f"Missing required fields for draft save:")
            logger.error(f"  - to: {to_email}")
            logger.error(f"  - subject: {subject}")
            logger.error(f"  - body: {body}")
            return HttpResponseBadRequest("to, subject, body are required")
        
        logger.info(f"Creating Gmail draft:")
        logger.info(f"  - To: {to_email}")
        logger.info(f"  - Subject: {subject}")
        logger.info(f"  - Body length: {len(body)}")
        
        draft_id = create_gmail_draft(service, to_email, subject, body)
        logger.info(f"Draft created successfully with ID: {draft_id}")
        return JsonResponse({"ok": True, "draft_id": draft_id})
    except Exception as e:
        logger.error(f"Save draft error: {str(e)}", exc_info=True)
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

########################################
# API: email details
########################################
@api_view(['GET'])
def email_detail_view(request, message_id):
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # First check if the email is available
        from .services.gmail import is_email_available, get_email_details
        
        if not is_email_available(service, message_id):
            return Response({'ok': False, 'error': 'Email not found or may have been deleted'}, status=status.HTTP_404_NOT_FOUND)
        
        email_details = get_email_details(service, message_id)
        if not email_details:
            return Response({'ok': False, 'error': 'Email not found or may have been deleted'}, status=status.HTTP_404_NOT_FOUND)
        
        # Add important status if user is authenticated
        if request.user.is_authenticated:
            email_details['is_important'] = ImportantEmail.objects.filter(
                user=request.user, 
                message_id=message_id
            ).exists()
        
        return Response({'ok': True, 'email': email_details})
    except Exception as e:
        logger.error(f"Error fetching email details: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: draft details
########################################
@api_view(['GET'])
def draft_detail_view(request, draft_id):
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate'}, status=status.HTTP_401_UNAUTHORIZED)
        
        from .services.gmail import get_draft_details
        draft_details = get_draft_details(service, draft_id)
        if not draft_details:
            return Response({'ok': False, 'error': 'Draft not found'}, status=status.HTTP_404_NOT_FOUND)
        
        return Response({'ok': True, 'draft': draft_details})
    except Exception as e:
        logger.error(f"Error fetching draft details: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: mark as read
########################################
@api_view(['POST'])
@csrf_exempt
def mark_as_read_view(request, message_id):
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate. Please reconnect your Gmail account.'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Log the attempt
        logger.info(f"Attempting to mark email {message_id} as read")
        
        success = mark_as_read(service, message_id)
        if success:
            # Clear the cache for unread emails to ensure fresh data
            try:
                # Try to clear by pattern if supported
                cache.delete_many([key for key in cache.keys('*gmail_unread_*')])
            except (AttributeError, NotImplementedError):
                # Fallback: clear entire cache if pattern deletion not supported
                cache.clear()
            
            logger.info(f"Successfully marked email {message_id} as read")
            return Response({'ok': True})
        else:
            logger.error(f"Failed to mark email {message_id} as read")
            return Response({'ok': False, 'error': 'Failed to mark email as read. The email may have been deleted or already marked as read.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Error marking email as read: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: bulk mark as read
########################################
@api_view(['POST'])
@csrf_exempt
def bulk_mark_as_read_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        email_ids = payload.get("email_ids", [])
        
        if not email_ids:
            return Response({'ok': False, 'error': 'No email IDs provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate. Please reconnect your Gmail account.'}, status=status.HTTP_401_UNAUTHORIZED)
        
        success_count = 0
        for email_id in email_ids:
            if mark_as_read(service, email_id):
                success_count += 1
        
        # Clear the cache for unread emails to ensure fresh data
        try:
            # Try to clear by pattern if supported
            cache.delete_many([key for key in cache.keys('*gmail_unread_*')])
        except (AttributeError, NotImplementedError):
            # Fallback: clear entire cache if pattern deletion not supported
            cache.clear()
        
        return Response({
            'ok': True, 
            'success_count': success_count,
            'total_count': len(email_ids)
        })
    except Exception as e:
        logger.error(f"Error in bulk_mark_as_read_view: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: delete draft
########################################
@api_view(['DELETE'])
@csrf_exempt
def delete_draft_view(request, draft_id):
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate'}, status=status.HTTP_401_UNAUTHORIZED)
        
        from .services.gmail import delete_draft
        success = delete_draft(service, draft_id)
        if success:
            return Response({'ok': True})
        else:
            return Response({'ok': False, 'error': 'Failed to delete draft'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Error deleting draft: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: update draft
########################################
@api_view(['PUT'])
@csrf_exempt
def update_draft_view(request, draft_id):
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return JsonResponse({"ok": False, "error": "Not authorized. Connect Gmail first."}, status=401)
        
        payload = json.loads(request.body.decode("utf-8"))
        to_email = payload.get("to")
        subject = payload.get("subject")
        body = payload.get("body")
        
        if not all([to_email, subject, body]):
            return HttpResponseBadRequest("to, subject, body are required")
        
        from .services.gmail import update_draft
        draft_id = update_draft(service, draft_id, to_email, subject, body)
        return JsonResponse({"ok": True, "draft_id": draft_id})
    except Exception as e:
        logger.error(f"Update draft error: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

########################################
# API: Archive email
########################################
@api_view(['POST'])
@csrf_exempt
def archive_email_view(request, message_id):
    """Archive a single email by removing the INBOX label"""
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate. Please reconnect your Gmail account.'}, 
                          status=status.HTTP_401_UNAUTHORIZED)
        
        # Log the attempt
        logger.info(f"Attempting to archive email {message_id}")
        
        # Archive by removing INBOX label
        success = archive_email(service, message_id)
        if success:
            # Clear the cache for unread emails to ensure fresh data
            try:
                # Try to clear by pattern if supported
                cache.delete_many([key for key in cache.keys('*gmail_unread_*')])
            except (AttributeError, NotImplementedError):
                # Fallback: clear entire cache if pattern deletion not supported
                cache.clear()
            
            logger.info(f"Successfully archived email {message_id}")
            return Response({'ok': True})
        else:
            logger.error(f"Failed to archive email {message_id}")
            return Response({'ok': False, 'error': 'Failed to archive email. The email may have been deleted or already archived.'}, 
                          status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Error archiving email: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: Bulk archive emails
########################################
@api_view(['POST'])
@csrf_exempt
def bulk_archive_emails_view(request):
    """Archive multiple emails by removing the INBOX label"""
    try:
        payload = json.loads(request.body.decode("utf-8"))
        email_ids = payload.get("email_ids", [])
        
        if not email_ids:
            return Response({'ok': False, 'error': 'No email IDs provided'}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return Response({'ok': False, 'error': 'Failed to authenticate. Please reconnect your Gmail account.'}, 
                          status=status.HTTP_401_UNAUTHORIZED)
        
        success_count = 0
        for email_id in email_ids:
            if archive_email(service, email_id):
                success_count += 1
        
        # Clear the cache for unread emails to ensure fresh data
        try:
            # Try to clear by pattern if supported
            cache.delete_many([key for key in cache.keys('*gmail_unread_*')])
        except (AttributeError, NotImplementedError):
            # Fallback: clear entire cache if pattern deletion not supported
            cache.clear()
        
        return Response({
            'ok': True, 
            'success_count': success_count,
            'total_count': len(email_ids)
        })
    except Exception as e:
        logger.error(f"Error in bulk_archive_emails_view: {str(e)}", exc_info=True)
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# Helper function: Archive email
########################################
def archive_email(service, message_id):
    """Archive an email by removing the INBOX label"""
    try:
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={
                'removeLabelIds': ['INBOX']
            }
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error archiving email {message_id}: {str(e)}")
        return False

########################################
# Logout (Updated to remove credentials)
########################################
def logout_view(request):
    # Remove stored Gmail credentials
    if request.user.is_authenticated:
        _save_creds_to_db(None, user=request.user)
    
    # Clear session data but don't delete the session key yet
    request.session.clear()
    request.session['logout_in_progress'] = True
    
    # Add success message
    messages.success(request, "You have been successfully logged out. You can now sign in with a different account.")
    
    return redirect('oauth_start')

########################################
# Force Re-authentication (Updated)
########################################
def force_reauth_view(request):
    """Force re-authentication with Google"""
    # Remove stored Gmail credentials if user is authenticated
    if request.user.is_authenticated:
        _save_creds_to_db(None, user=request.user)
    
    # Clear all session data
    request.session.flush()
    
    # Add a message to inform the user
    messages.info(request, "All authentication data cleared. Please select your Google account and grant Calendar access permissions.")
    
    # Redirect to OAuth start
    return redirect('oauth_start')

########################################
# API: Schedule Meeting
########################################
@csrf_exempt
@require_POST
def schedule_meeting_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        email_id = payload.get("email_id")
        title = payload.get("title")
        description = payload.get("description")
        start_datetime = payload.get("start_datetime")
        end_datetime = payload.get("end_datetime")
        attendees = payload.get("attendees", [])
        reminders = payload.get("reminders", [])  # Get reminders from payload
        
        if not all([title, start_datetime, end_datetime]):
            return HttpResponseBadRequest("Title, start datetime, and end datetime are required")
        
        # Get Gmail service
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return JsonResponse({"ok": False, "error": "Not authorized. Connect Gmail first."}, status=401)
        
        # Get credentials
        creds = _load_creds_from_db(user=request.user if request.user.is_authenticated else None)
        if not creds:
            return JsonResponse({"ok": False, "error": "Not authorized. Connect Gmail first."}, status=401)
        
        # Check if Calendar scope is included
        if 'https://www.googleapis.com/auth/calendar.events' not in creds.scopes:
            return JsonResponse({
                "ok": False, 
                "error": "Calendar access permission not granted. Please re-authenticate with Google and grant Calendar access.",
                "needs_reauth": True
            }, status=403)
        
        # Build the Calendar service
        from googleapiclient.discovery import build
        calendar_service = build('calendar', 'v3', credentials=creds)
        
        # Create event
        event = {
            'summary': title,
            'description': description,
            'start': {
                'dateTime': start_datetime,
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_datetime,
                'timeZone': 'UTC',
            },
        }
        
        # Add attendees if provided
        if attendees:
            event['attendees'] = [{'email': email} for email in attendees]
        
        # Add reminders if provided
        if reminders:
            event['reminders'] = {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': minutes} for minutes in reminders
                ]
            }
        else:
            # Use default reminders if none specified
            event['reminders'] = {
                'useDefault': True
            }
        
        # Insert the event
        event_result = calendar_service.events().insert(
            calendarId='primary',
            body=event,
            sendUpdates='all'  # Send notifications to attendees
        ).execute()
        
        return JsonResponse({
            "ok": True,
            "html_link": event_result.get('htmlLink'),
        })
    except Exception as e:
        logger.error(f"Schedule meeting error: {str(e)}")
        
        # Check if the error is related to insufficient scopes
        if "insufficient authentication scopes" in str(e) or "Insufficient Permission" in str(e):
            return JsonResponse({
                "ok": False, 
                "error": "Calendar access permission not granted. Please re-authenticate with Google and grant Calendar access.",
                "needs_reauth": True
            }, status=403)
        
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

########################################
# API: Check Calendar Permissions
########################################
@api_view(['GET'])
def check_calendar_permissions_view(request):
    """Check if the user has granted Calendar permissions"""
    try:
        creds = _load_creds_from_db(user=request.user if request.user.is_authenticated else None)
        if not creds:
            return Response({
                'ok': False,
                'has_calendar_permissions': False,
                'error': 'Not authenticated'
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        has_calendar_permissions = 'https://www.googleapis.com/auth/calendar.events' in creds.scopes
        
        return Response({
            'ok': True,
            'has_calendar_permissions': has_calendar_permissions
        })
    except Exception as e:
        logger.error(f"Error checking calendar permissions: {str(e)}")
        return Response({
            'ok': False,
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: Voice Command Help
########################################
@api_view(['GET'])
def voice_command_help_view(request):
    """Returns list of available voice commands for help display"""
    try:
        commands = [
            {"command": "load emails", "description": "Load or refresh your emails"},
            {"command": "mark all as read", "description": "Mark all emails as read"},
            {"command": "mark as read", "description": "Mark the current email as read"},
            {"command": "archive", "description": "Archive the current email"},
            {"command": "delete", "description": "Delete the current email"},
            {"command": "reply", "description": "Reply to the current email"},
            {"command": "compose", "description": "Compose a new email"},
            {"command": "next page", "description": "Go to the next page of emails"},
            {"command": "previous page", "description": "Go to the previous page of emails"},
            {"command": "generate reply", "description": "Generate an AI reply for the current email"},
            {"command": "save draft", "description": "Save the current draft"},
            {"command": "schedule meeting", "description": "Schedule a meeting"},
            {"command": "toggle theme", "description": "Toggle between dark and light theme"},
            {"command": "help", "description": "Show available commands"},
        ]
        return Response({'ok': True, 'commands': commands})
    except Exception as e:
        logger.error(f"Error fetching voice command help: {str(e)}")
        return Response({'ok': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

########################################
# API: Voice Command (Updated)
########################################
@csrf_exempt
@require_POST
def voice_command_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        command = payload.get("command", "").lower().strip()
        
        if not command:
            return JsonResponse({"ok": False, "error": "No command provided"}, status=400)
        
        # Process the command and return the action to execute
        action = process_voice_command(command)
        
        # Special handling for help command
        if action.get("type") == "help":
            # Get the list of commands for voice output
            help_response = voice_command_help_view(request)
            if help_response.status_code == 200:
                commands_data = help_response.data.get('commands', [])
                # Format commands for voice output
                voice_text = "Available voice commands are: "
                for cmd in commands_data:
                    voice_text += f"{cmd['command']}. "
                action["voice_output"] = voice_text
                action["commands_list"] = commands_data
        
        return JsonResponse({
            "ok": True, 
            "action": action,
            "command": command,
            "message": f"Command recognized: {command}"
        })
    except Exception as e:
        logger.error(f"Voice command error: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

def process_voice_command(command):
    """Process voice command and return the corresponding action"""
    
    # Load emails / refresh
    if any(keyword in command for keyword in ["load emails", "refresh", "show emails", "get emails"]):
        return {"type": "load_emails"}
    
    # Mark all as read
    elif any(keyword in command for keyword in ["mark all as read", "mark everything as read", "read all"]):
        return {"type": "mark_all_as_read"}
    
    # Mark current email as read
    elif any(keyword in command for keyword in ["mark as read", "mark this as read", "read this"]):
        return {"type": "mark_current_as_read"}
    
    # Archive email
    elif any(keyword in command for keyword in ["archive", "archive this", "archive email"]):
        return {"type": "archive_current"}
    
    # Delete email
    elif any(keyword in command for keyword in ["delete", "delete this", "remove"]):
        return {"type": "delete_current"}
    
    # Reply to email
    elif any(keyword in command for keyword in ["reply", "reply to this", "reply to email"]):
        return {"type": "reply_current"}
    
    # Compose new email
    elif any(keyword in command for keyword in ["compose", "new email", "write email", "create email"]):
        return {"type": "compose_email"}
    
    # Next page
    elif any(keyword in command for keyword in ["next page", "show next", "go to next"]):
        return {"type": "next_page"}
    
    # Previous page
    elif any(keyword in command for keyword in ["previous page", "show previous", "go back"]):
        return {"type": "previous_page"}
    
    # Generate reply
    elif any(keyword in command for keyword in ["generate reply", "create reply", "ai reply"]):
        return {"type": "generate_reply"}
    
    # Save draft
    elif any(keyword in command for keyword in ["save draft", "save as draft"]):
        return {"type": "save_draft"}
    
    # Schedule meeting
    elif any(keyword in command for keyword in ["schedule meeting", "create meeting", "new meeting"]):
        return {"type": "schedule_meeting"}
    
    # Toggle theme
    elif any(keyword in command for keyword in ["toggle theme", "switch theme", "dark mode", "light mode"]):
        return {"type": "toggle_theme"}
    
    # Help
    elif any(keyword in command for keyword in ["help", "what can i say", "commands"]):
        return {"type": "help"}
    
    # Unknown command
    else:
        return {"type": "unknown", "message": "Command not recognized"}

########################################
# API: Test Gmail drafts (NEW)
########################################
@api_view(['GET'])
def test_gmail_drafts_view(request):
    """Test endpoint to check Gmail drafts"""
    try:
        user = request.user if request.user.is_authenticated else None
        service = get_gmail_service(user=user)
        
        if not service:
            return Response({'error': 'No Gmail service available'}, status=400)
        
        # Get profile
        profile = service.users().getProfile(userId='me').execute()
        logger.info(f"Testing Gmail drafts for: {profile.get('emailAddress')}")
        
        # Get all drafts
        drafts_response = service.users().drafts().list(userId='me').execute()
        draft_ids = [draft['id'] for draft in drafts_response.get('drafts', [])]
        
        logger.info(f"Found {len(draft_ids)} Gmail drafts")
        
        # Get details for first few drafts
        draft_details = []
        for i, draft_id in enumerate(draft_ids[:5]):  # Limit to first 5 for testing
            try:
                draft = service.users().drafts().get(userId='me', id=draft_id).execute()
                message = draft['message']
                headers = message.get('payload', {}).get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(no subject)')
                to_email = next((h['value'] for h in headers if h['name'] == 'To'), 'Unknown')
                date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
                snippet = message.get('snippet', '')
                
                draft_details.append({
                    'id': draft_id,
                    'subject': subject,
                    'to': to_email,
                    'date': date,
                    'snippet': snippet
                })
                logger.info(f"Draft {i+1}: {subject[:50]}...")
                
            except Exception as e:
                logger.error(f"Error fetching draft {draft_id}: {str(e)}")
                continue
        
        return Response({
            'total_drafts': len(draft_ids),
            'draft_details': draft_details,
            'email': profile.get('emailAddress')
        })
    except Exception as e:
        logger.error(f"Error in test_gmail_drafts_view: {str(e)}", exc_info=True)
        return Response({'error': str(e)}, status=500)

########################################
# API: edit Gmail draft (NEW)
########################################
@api_view(['PUT'])
@csrf_exempt
def edit_gmail_draft_view(request, draft_id):
    """Edit a Gmail draft (subject and recipient only)"""
    try:
        service = get_gmail_service(user=request.user if request.user.is_authenticated else None)
        if not service:
            return JsonResponse({"ok": False, "error": "Not authorized. Connect Gmail first."}, status=401)
        
        payload = json.loads(request.body.decode("utf-8"))
        new_subject = payload.get("subject")
        new_to = payload.get("to")
        
        if not new_subject or not new_to:
            return HttpResponseBadRequest("Subject and recipient are required")
        
        logger.info(f"Editing Gmail draft {draft_id}:")
        logger.info(f"  - New subject: {new_subject}")
        logger.info(f"  - New recipient: {new_to}")
        
        # Get current draft details
        from .services.gmail import get_draft_details
        current_draft = get_draft_details(service, draft_id)
        if not current_draft:
            return Response({"ok": False, "error": "Draft not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Create updated message with new subject and recipient, keeping original body
        from .services.gmail import create_message
        message = create_message(new_to, new_subject, current_draft.get('body', ''))
        
        # Update the draft
        updated_draft = service.users().drafts().update(
            userId='me',
            id=draft_id,
            body=message
        ).execute()
        
        logger.info(f"Draft {draft_id} updated successfully")
        return JsonResponse({
            "ok": True,
            "draft_id": updated_draft.get('id'),
            "subject": new_subject,
            "to": new_to
        })
    except Exception as e:
        logger.error(f"Error editing Gmail draft: {str(e)}", exc_info=True)
        return JsonResponse({"ok": False, "error": str(e)}, status=500)
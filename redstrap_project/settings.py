"""
Django settings for redstrap_project project.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file (must be in project root, same level as manage.py)
env_path = BASE_DIR / '.env'
load_dotenv(dotenv_path=env_path)


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-change-this-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'redstrap_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'redstrap_project.wsgi.application'


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Login URLs
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# RapidAPI Configuration
# Support multiple API keys for load balancing and rate limit management
RAPIDAPI_KEYS = [
    'a8fda11e76msh91790916e41cd1ep1d15b6jsn684a53a759bf',
    'd8f6b4111bmsh74a2dd675cd6767p124173jsna55fffc313d6',
    '19408733bamsha9cd9d39c52f78dp1fb914jsnbc5ee6c6a61a',
    'd409935002msh6ca472652d2ea6ep143efcjsn4d350435d328',
    #sallu's keys
    '39d4dc717fmsh78d577788d2a3a6p161679jsn5e4efe64ee0b',
    'bacf8822a1msh8016f4f6b538841p12cfbbjsn537ac3fe0e64',
    '2e58497092msh34920d58ae43ee2p19c8ecjsna42f6519a892',
    '6c9bb0a034msha5c2cd9f2e24080p1bb7cdjsnd04ddf1fc7b0',
    '05f131b61cmshe63b7bc03e266e4p16239bjsnf6b3cb76ffe2',
    '8e9284a02emsh6d316c5682fa1dfp15b103jsn075b2b6505e9',
    '455f886804msh5720a293836962bp1c54abjsnc8d7a0af2028',
    '970fe3d582msh54c325dab9e922cp1fc3cdjsn950bef760cf',
    'a9d39e5f2dmsh8b33c5111ac35dep18f68ejsn07e38e0beeff'
]
# Fallback to single key from environment if provided (for backward compatibility)
SINGLE_RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '')
if SINGLE_RAPIDAPI_KEY and SINGLE_RAPIDAPI_KEY not in RAPIDAPI_KEYS:
    RAPIDAPI_KEYS.append(SINGLE_RAPIDAPI_KEY)
# Use first key as default for backward compatibility
RAPIDAPI_KEY = RAPIDAPI_KEYS[0] if RAPIDAPI_KEYS else ''
RAPIDAPI_HOST = 'instagram120.p.rapidapi.com'

# Debug: Save API JSON responses to files for analysis
# Set to True to save all API responses to debug_responses/ directory
# Useful for debugging API structure changes or analyzing response formats
DEBUG_SAVE_RESPONSES = os.environ.get('DEBUG_SAVE_RESPONSES', 'False').lower() == 'true'
# Maximum number of response files to keep per endpoint type (prevents disk space issues)
DEBUG_MAX_RESPONSE_FILES = int(os.environ.get('DEBUG_MAX_RESPONSE_FILES', '50'))

# Skip reels endpoint for play_count fetching (if rate limits are an issue)
# When True, only uses posts endpoint and fallback extraction for play_count
# This reduces API calls by 50% but may result in missing play_count data
SKIP_REELS_ENDPOINT_FOR_PLAY_COUNT = os.environ.get('SKIP_REELS_ENDPOINT_FOR_PLAY_COUNT', 'False').lower() == 'true'

# Test mode: Limit reel fetching to 10 recent reels for testing purposes
# When True, fetch_instagram_reels() will only return the 10 most recent reels
TEST_MODE_REELS_LIMIT = int(os.environ.get('TEST_MODE_REELS_LIMIT', '10'))

# Test mode: Limit post fetching to 600 recent posts for testing purposes
# Set to None or 0 to disable test mode and fetch all posts
TEST_MODE_POSTS_LIMIT = int(os.environ.get('TEST_MODE_POSTS_LIMIT', '600'))

# Test mode: Limit page fetching to 50 pages for testing purposes
# Set to None or 0 to disable page limit and fetch all pages
TEST_MODE_PAGES_LIMIT = int(os.environ.get('TEST_MODE_PAGES_LIMIT', '50'))

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'core': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'core.services': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}


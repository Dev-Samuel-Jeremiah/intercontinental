"""
lms_project/settings.py — School management system (dedicated single-school install)

Everything school-specific comes from .env — see .env.example.
"""
import sys
from pathlib import Path
from decouple import Config, RepositoryEnv, Csv

BASE_DIR = Path(__file__).resolve().parent.parent
config = Config(RepositoryEnv(BASE_DIR / ".env"))

# ── Django Core ──────────────────────────────────────────────────
SECRET_KEY = config('SECRET_KEY')
DEBUG      = config('DEBUG', default=False, cast=bool)
if not DEBUG and (SECRET_KEY in ('', 'change-me') or len(SECRET_KEY) < 40):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Set a real SECRET_KEY in .env before running with DEBUG=False '
                               '(see .env.example for the command that generates one).')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

# ── AI keys ──────────────────────────────────────────────────────
# NOTE: previously loaded twice (once via python-dotenv/os.getenv, once
# via decouple). Both were reading the same .env file, so this is
# consolidated to a single source of truth (decouple).
OPENAI_API_KEY = config('OPENAI_API_KEY', default='')
GEMINI_API_KEY = config('GEMINI_API_KEY', default='')
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY', default='')
# Which AI to try first ('openai', 'gemini' or 'claude'); the others are fallbacks
# when they have a key. Schools can override this per feature under AI settings.
AI_DEFAULT_PROVIDER = config('AI_DEFAULT_PROVIDER', default='openai')
OPENAI_MODEL    = config('OPENAI_MODEL', default='gpt-4o-mini')
GEMINI_MODEL    = config('GEMINI_MODEL', default='gemini-2.0-flash')
ANTHROPIC_MODEL = config('ANTHROPIC_MODEL', default='claude-sonnet-4-20250514')
# Default monthly AI allowance per school, in tokens (about 750 words per 1,000 tokens).
AI_MONTHLY_TOKENS = config('AI_MONTHLY_TOKENS', default=300000, cast=int)

# ── The school ──────────────────────────────────────────────────
# This installation runs one school. SCHOOL_SLUG is the short code it is
# stored under (created by `python manage.py setup_school`).
SCHOOL_NAME = config('SCHOOL_NAME', default='Intercontinental School Kano')
SCHOOL_SLUG = config('SCHOOL_SLUG', default='intercontinental')
# The portal's own domain, e.g. portal.intercontinentalschool.edu.ng
ROOT_DOMAIN = config('ROOT_DOMAIN', default='localhost')
# Port used in local development (e.g. 8000). Leave blank in production.
DEV_PORT    = config('DEV_PORT', default='')
_scheme = 'http' if ROOT_DOMAIN in ('localhost', '127.0.0.1') else 'https'
# Full address used in emails, SMS, QR codes and links, e.g. https://portal.example.edu.ng
SITE_URL = config('SITE_URL', default=f"{_scheme}://{ROOT_DOMAIN}{':' + DEV_PORT if DEV_PORT else ''}").rstrip('/')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'widget_tweaks',
    # School core (the single school record, branding, sections, backups)
    'tenants.apps.TenantsConfig',
    'admissions',
    'safety',
    'curriculum',
    # School apps
    'users',
    'academics.apps.AcademicsConfig',
    'cbt',
    'dashboard',
    'results',
    'classroom',
    'inventory',
    'announcement',
    'timetable',
    'finance',
    'payroll',
    'hostel',
    'communications',
    'idcards',
    'assistant',
    'analytics',
    'hr',
    'family',
    'studentlife',
    'teaching',
    'transport',
    'library',
    'welfare',
    'facilities',
    'saas_admin',
    'security.apps.SecurityConfig',
    'notifications.apps.NotificationsConfig',
    'staffchat.apps.StaffchatConfig',
    'moments.apps.MomentsConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'users.audit.CurrentUserMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'tenants.middleware.TenantMiddleware',
    'lms_project.middleware.OneSessionPerBrowserMiddleware',
    'security.middleware.LoginThrottleMiddleware',
    'security.middleware.SecurityGateMiddleware',
    'security.middleware.SecurityHeadersMiddleware',
]

ROOT_URLCONF = 'lms_project.tenant_urls'
# Branded page when a form's CSRF check fails (e.g. session expired)
CSRF_FAILURE_VIEW = 'lms_project.error_views.csrf_failure'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'academics.context_processors.active_academic_context',
        'tenants.context_processors.tenant_context',
        'saas_admin.context_processors.platform',
    ]},
}]

WSGI_APPLICATION = 'lms_project.wsgi.application'

# ── Database ─────────────────────────────────────────────────────
# Switches automatically based on DB_ENGINE in .env.
#   DB_ENGINE=sqlite       -> local dev (default, zero setup)
#   DB_ENGINE=postgresql   -> production
DB_ENGINE = config('DB_ENGINE', default='sqlite')

if DB_ENGINE == 'postgresql':
    DATABASES = {'default': {
        'ENGINE':   'django.db.backends.postgresql',
        'NAME':     config('DB_NAME'),
        'USER':     config('DB_USER'),
        'PASSWORD': config('DB_PASSWORD'),
        'HOST':     config('DB_HOST', default='localhost'),
        'PORT':     config('DB_PORT', default='5432'),
    }}
else:
    # SQLite, tuned for a live site with several Gunicorn workers:
    #   WAL           readers never block the writer, and the writer never blocks readers
    #   IMMEDIATE     write transactions take the lock up front, so concurrent writers
    #                 queue (up to `timeout`) instead of failing with "database is locked"
    #   busy_timeout  how long a writer waits for the lock
    # In production keep the file outside the code folder (SQLITE_PATH), e.g.
    # /srv/intercontinental/data/db.sqlite3, so deploys never touch it.
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Path(config('SQLITE_PATH', default=str(BASE_DIR / 'db.sqlite3'))),
        'OPTIONS': {
            'timeout': 20,
            'transaction_mode': 'IMMEDIATE',
            'init_command': ('PRAGMA journal_mode=WAL;'
                             'PRAGMA synchronous=NORMAL;'
                             'PRAGMA busy_timeout=20000;'
                             'PRAGMA foreign_keys=ON;'
                             'PRAGMA cache_size=-20000;'),
        },
    }}

# ── Cache ───────────────────────────────────────────────────────────────────
# Set REDIS_URL to share one cache across all Gunicorn workers. Without it we
# fall back to a per-process cache, which is fine for a single worker but means
# each worker caches tenants separately (status changes can take a minute to
# show up on every worker).
# ── Running the test suite ──────────────────────────────────────────────────
# Never let `manage.py test` reach a real email or SMS provider: a developer
# machine with live keys would otherwise message real parents (and bill for it).
TESTING = 'test' in sys.argv
SMS_ENABLED = config('SMS_ENABLED', default=True, cast=bool)
# Secret part of the SMS delivery-report webhook URL: /communications/dlr/<token>/ (empty = webhook off)
SMS_DLR_TOKEN = config('SMS_DLR_TOKEN', default='')
# SMS units (F246). Leave SMS_METERING off: the school pays its SMS provider directly.
SMS_METERING = config('SMS_METERING', default=False, cast=bool)
SMS_UNIT_PRICE = config('SMS_UNIT_PRICE', default='4.00')        # naira per unit
SMS_FREE_UNITS = config('SMS_FREE_UNITS', default=0, cast=int)   # given to each school once
SMS_LOW_UNITS = config('SMS_LOW_UNITS', default=200, cast=int)   # warn admins below this
SMS_BUNDLES = [500, 1000, 5000, 10000]
# WhatsApp Business Cloud API (F241) — from developers.facebook.com → your app → WhatsApp
WHATSAPP_TOKEN = config('WHATSAPP_TOKEN', default='')
WHATSAPP_PHONE_NUMBER_ID = config('WHATSAPP_PHONE_NUMBER_ID', default='')
WHATSAPP_VERIFY_TOKEN = config('WHATSAPP_VERIFY_TOKEN', default='')   # any text; also typed into Meta's webhook form
WHATSAPP_APP_SECRET = config('WHATSAPP_APP_SECRET', default='')       # checks webhook signatures
# Secret part of the inbound SMS webhook URL: /hooks/sms-inbound/<token>/ (empty = off)
SMS_INBOUND_TOKEN = config('SMS_INBOUND_TOKEN', default='')
if TESTING:
    WHATSAPP_TOKEN = WHATSAPP_PHONE_NUMBER_ID = WHATSAPP_APP_SECRET = ''
    SMS_METERING = False
if TESTING:
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    SMS_ENABLED = False
    # Tests create hundreds of users; the production hasher is deliberately
    # slow, which made the suite take many minutes. Never used outside tests.
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    # Nor a real AI provider (cost, and flaky tests). Tests that need AI set a
    # fake key and replace the network call.
    OPENAI_API_KEY = GEMINI_API_KEY = ANTHROPIC_API_KEY = ''

# ── Background work ─────────────────────────────────────────────────────────
# Bulk email/SMS runs on a small thread pool so requests return immediately.
# RUN_TASKS_EAGERLY=True runs them inline (used by the test suite).
BACKGROUND_WORKERS  = config('BACKGROUND_WORKERS', default=4, cast=int)
RUN_TASKS_EAGERLY   = config('RUN_TASKS_EAGERLY', default=False, cast=bool)

REDIS_URL = config('REDIS_URL', default='')
if REDIS_URL:
    CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'KEY_PREFIX': config('CACHE_PREFIX', default=SCHOOL_SLUG),
    }}
else:
    CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'school-local',
    }}

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 1209600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_DOMAIN = config('SESSION_COOKIE_DOMAIN', default=None)
CSRF_COOKIE_DOMAIN    = config('SESSION_COOKIE_DOMAIN', default=None)

# Must be False in dev — cookies over plain http on localhost won't be
# sent by the browser if Secure=True
SESSION_COOKIE_SECURE   = False if DEBUG else True
CSRF_COOKIE_SECURE      = False if DEBUG else True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE    = 'Lax'
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = False if DEBUG else True

# Nginx terminates TLS and proxies to Gunicorn over plain HTTP.
# Without this, Django can't tell the request was HTTPS, and
# SECURE_SSL_REDIRECT=True above will cause an infinite redirect loop.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = 'DENY'

# Trust both the production domain and localhost for local dev
_is_localhost = ROOT_DOMAIN in ('localhost', '127.0.0.1')
if _is_localhost:
    CSRF_TRUSTED_ORIGINS = [
        'http://localhost:8000',
        'http://*.localhost:8000',
        'http://localhost',
        'http://*.localhost',
    ]
else:
    CSRF_TRUSTED_ORIGINS = [
        f'https://{ROOT_DOMAIN}',
        f'https://*.{ROOT_DOMAIN}',
        f'https://www.{ROOT_DOMAIN}',
    ]

AUTH_USER_MODEL = 'users.User'
LOGIN_URL = '/staff/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
# Usernames are unique per school (users.User.Meta.constraints), not
# platform-wide; TenantUserManager.get_by_natural_key resolves logins.
SILENCED_SYSTEM_CHECKS = ['auth.W004']

AUTHENTICATION_BACKENDS = [
    'users.backends.ParentBackend',
    'django.contrib.auth.backends.ModelBackend',
]
# ── Master passwords (legacy) ────────────────────────────────────────────────
# One shared password that signs in as ANY student / ANY parent. A single leak
# exposes every account on every school, so this is OFF unless a deployment
# explicitly turns it back on. Students and parents each have their own
# password (see users/passwords.py), which is what the login pages use.
ALLOW_MASTER_PASSWORDS  = config('ALLOW_MASTER_PASSWORDS', default=False, cast=bool)
MASTER_STUDENT_PASSWORD = config('MASTER_STUDENT_PASSWORD', default='')
MASTER_PARENT_PASSWORD  = config('MASTER_PARENT_PASSWORD', default='')

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    {'NAME': 'security.validators.SchoolPasswordPolicy'},   # each school's own rules (F447)
]

# ── Security, privacy & compliance (security app) ───────────────────────────
# Two-step sign-in: send users the school requires it for to the set-up page.
# (Off under tests, which sign users in directly; the two-step tests switch it on themselves.)
TWO_FACTOR_ENFORCE = config('TWO_FACTOR_ENFORCE', default=True, cast=bool) and not TESTING
PLATFORM_2FA = config('PLATFORM_2FA', default='ADMINS')          # NONE | ADMINS for platform superusers
# Encrypts medical, counselling, safeguarding and support notes (F453). Set it and keep a copy safe.
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default='')
# Signed links for personal files (F459)
SIGNED_MEDIA_HOURS = config('SIGNED_MEDIA_HOURS', default=6, cast=int)
SIGNED_MEDIA_ACCEL = config('SIGNED_MEDIA_ACCEL', default='')     # e.g. /protected-media/ with nginx
# Content-Security-Policy (F457). CSP_REPORT_ONLY=True to try it without blocking anything.
CSP_ENABLED = config('CSP_ENABLED', default=True, cast=bool)
CSP_REPORT_ONLY = config('CSP_REPORT_ONLY', default=False, cast=bool)
CSP_EXTRA = {}            # e.g. {'script-src': ['https://example.com']}
SECURITY_CONTACT_EMAIL = config('SECURITY_CONTACT_EMAIL', default='')
# Extra addresses (comma-separated) for technical and security alerts, besides ADMIN_EMAIL.
IT_ALERT_EMAILS = config('IT_ALERT_EMAILS', default='')

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Lagos'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = Path(config('STATIC_ROOT', default=str(BASE_DIR / 'staticfiles')))
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(config('MEDIA_ROOT', default=str(BASE_DIR / 'mediafiles')))
# Where backup_database writes database snapshots (keep a copy off the server too)
BACKUP_DIR = Path(config('BACKUP_DIR', default=str(BASE_DIR / 'backups')))
BACKUP_KEEP_DAYS = config('BACKUP_KEEP_DAYS', default=14, cast=int)
# Files that must never be reachable by URL — student documents such as birth
# certificates and medical reports. Served only through permission-checked views.
PRIVATE_MEDIA_ROOT = Path(config('PRIVATE_MEDIA_ROOT', default=str(BASE_DIR / 'private_media')))
MAX_UPLOAD_SIZE = 5242880
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = f"{config('EMAIL_FROM_NAME', default=SCHOOL_NAME)} <{config('EMAIL_HOST_USER')}>"
# Without a timeout, a blocked/slow SMTP connection makes send_mail() hang
# for the entire request. This bounds it to 10s.
EMAIL_TIMEOUT = config('EMAIL_TIMEOUT', default=10, cast=int)


# Where Django emails you when a 500 error happens in production
# (requires DEBUG=False and a working EMAIL_* config above).
ADMINS = [('School IT', config('ADMIN_EMAIL', default=config('EMAIL_HOST_USER')))]
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# The school's own Paystack account — online fee and admission-form payments go here.
PAYSTACK_SECRET_KEY = config('PAYSTACK_SECRET_KEY', default='')
PAYSTACK_PUBLIC_KEY = config('PAYSTACK_PUBLIC_KEY', default='')

TERMII_API_KEY   = config('TERMII_API_KEY', default='')
TERMII_SENDER_ID = config('TERMII_SENDER_ID', default='')
AT_API_KEY       = config('AT_API_KEY',  default='')
AT_USERNAME      = config('AT_USERNAME', default='sandbox')
AT_SENDER_ID     = config('AT_SENDER_ID', default=None)

SCHOOL_PHONE      = config('SCHOOL_PHONE', default='')
SCHOOL_PORTAL_URL = config('SCHOOL_PORTAL_URL', default=SITE_URL)

# ── Logging ──────────────────────────────────────────────────────
# Errors go to console (captured by systemd/journalctl in production)
# instead of silently disappearing when DEBUG=False.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
if TESTING:
    # Uploaded and generated files from tests go to a throwaway folder,
    # never into the real media directories.
    import tempfile as _tempfile
    MEDIA_ROOT = Path(_tempfile.mkdtemp(prefix='school-test-media-'))
    PRIVATE_MEDIA_ROOT = Path(_tempfile.mkdtemp(prefix='school-test-private-'))

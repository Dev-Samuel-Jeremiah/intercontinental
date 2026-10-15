from .models import AcademicSession, Term

def get_active_session():
    return AcademicSession.objects.filter(is_active=True).first()

def get_active_term():
    return Term.objects.filter(is_active=True).first()

def promotion_is_locked():
    session = get_active_session()
    term = get_active_term()
    return bool(session and term)

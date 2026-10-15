from .models import AcademicSession, Term

def active_academic_context(request):
    return {
        'active_session': AcademicSession.objects.filter(is_active=True).first(),
        'active_term': Term.objects.filter(is_active=True).first(),
    }

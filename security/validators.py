"""
security/validators.py
──────────────────────
The school's password rules (F447) as a Django password validator, so every
place that checks passwords with `validate_password` follows them: length,
letters and numbers, a symbol, and not reusing recent passwords.
"""
from django.core.exceptions import ValidationError


class SchoolPasswordPolicy:

    def validate(self, password, user=None):
        from .services import policy_problems
        problems = policy_problems(password, user)
        if problems:
            raise ValidationError(problems, code='password_policy')

    def get_help_text(self):
        return 'Your password must follow your school\'s password rules.'

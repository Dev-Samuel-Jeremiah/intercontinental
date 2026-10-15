"""Deployment self-check: DEBUG, keys, HTTPS cookies, master passwords, 2FA on platform accounts, public files."""
from django.core.management.base import BaseCommand

from security import services as svc


class Command(BaseCommand):
    help = 'Check the deployment for common security problems.'

    def handle(self, *args, **opts):
        problems = 0
        for level, text in svc.security_check():
            style = {'bad': self.style.ERROR, 'warn': self.style.WARNING, 'ok': self.style.SUCCESS}[level]
            self.stdout.write(style(f'[{level.upper():4}] {text}'))
            problems += level == 'bad'
        if problems:
            raise SystemExit(1)

"""Move personal files (passports, proofs, receipts, lesson files…) out of the public media folder (F459)."""
from django.core.management.base import BaseCommand

from security.media import move_legacy_files, protected_fields


class Command(BaseCommand):
    help = 'Move files of signed-link fields from MEDIA_ROOT into the private signed folder.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        fields = protected_fields()
        n = move_legacy_files(fields, dry_run=opts['dry_run'])
        verb = 'would be moved' if opts['dry_run'] else 'moved'
        self.stdout.write(self.style.SUCCESS(f'{n} files {verb} ({len(fields)} file fields checked).'))

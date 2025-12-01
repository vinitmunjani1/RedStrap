"""
Management command to clean Instagram usernames (remove whitespace, convert to lowercase).
"""
from django.core.management.base import BaseCommand
from core.models import InstagramAccount


class Command(BaseCommand):
    help = 'Clean Instagram usernames by removing whitespace and converting to lowercase'

    def handle(self, *args, **options):
        accounts = InstagramAccount.objects.all()
        cleaned_count = 0
        
        for account in accounts:
            original_username = account.username
            cleaned_username = original_username.strip().lstrip('@').lower()
            
            if cleaned_username != original_username:
                # Check if cleaned username already exists
                existing = InstagramAccount.objects.filter(
                    username=cleaned_username,
                    user=account.user
                ).exclude(id=account.id).first()
                
                if existing:
                    self.stdout.write(
                        self.style.WARNING(
                            f'Skipping @{original_username} -> @{cleaned_username} (duplicate exists)'
                        )
                    )
                else:
                    account.username = cleaned_username
                    account.save()
                    cleaned_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'Cleaned: @{original_username} -> @{cleaned_username}')
                    )
        
        self.stdout.write(
            self.style.SUCCESS(f'Cleaned {cleaned_count} usernames')
        )


"""
Management command to create a superuser with explicit permissions.
This ensures the user has is_staff=True and is_superuser=True flags set.
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import transaction


class Command(BaseCommand):
    help = 'Create a superuser with explicit staff and superuser permissions'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, help='Username for the superuser')
        parser.add_argument('--email', type=str, help='Email for the superuser')
        parser.add_argument('--password', type=str, help='Password for the superuser')
        parser.add_argument('--noinput', action='store_true', help='Use provided arguments without prompting')

    def handle(self, *args, **options):
        username = options.get('username')
        email = options.get('email')
        password = options.get('password')
        noinput = options.get('noinput', False)

        if not noinput:
            # Interactive mode
            username = username or input('Username: ')
            email = email or input('Email address: ')
            password = password or input('Password: ')

        if not username or not password:
            self.stdout.write(self.style.ERROR('Username and password are required'))
            return

        # Check if user already exists
        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f'User "{username}" already exists. Updating permissions...'))
            user = User.objects.get(username=username)
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            if email:
                user.email = email
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Successfully updated user "{username}" with superuser permissions'))
        else:
            # Create new superuser
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email=email or '',
                    password=password,
                    is_staff=True,
                    is_superuser=True,
                    is_active=True
                )
                self.stdout.write(self.style.SUCCESS(f'Successfully created superuser "{username}"'))

        # Verify the user
        user = User.objects.get(username=username)
        self.stdout.write(f'\nUser Details:')
        self.stdout.write(f'  Username: {user.username}')
        self.stdout.write(f'  Email: {user.email}')
        self.stdout.write(f'  is_staff: {user.is_staff}')
        self.stdout.write(f'  is_superuser: {user.is_superuser}')
        self.stdout.write(f'  is_active: {user.is_active}')


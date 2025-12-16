"""
Django project initialization.
This file is executed when Django starts, so we can apply patches here.
"""

# Monkey patch to fix Python 3.14 compatibility issue with Django 4.2
# The issue is in django.template.context.Context.__copy__ method
import sys

if sys.version_info >= (3, 14):
    try:
        from django.template.context import Context
        
        # Store the original __copy__ method
        _original_context_copy = Context.__copy__
        
        def _patched_context_copy(self):
            """Patched __copy__ method that works with Python 3.14."""
            # Create a new Context instance
            # We avoid calling super().__copy__() which causes the Python 3.14 issue
            new_context = object.__new__(Context)
            # Manually initialize the dicts attribute
            new_context.dicts = list(self.dicts) if hasattr(self, 'dicts') and self.dicts else []
            # Copy other attributes that Context might have
            new_context.autoescape = getattr(self, 'autoescape', None)
            new_context.current_app = getattr(self, 'current_app', None)
            new_context.use_l10n = getattr(self, 'use_l10n', None)
            new_context.use_tz = getattr(self, 'use_tz', None)
            return new_context
        
        # Apply the patch
        Context.__copy__ = _patched_context_copy
    except (ImportError, AttributeError):
        # If the patch fails, continue anyway
        pass


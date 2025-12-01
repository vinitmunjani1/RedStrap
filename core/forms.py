"""
Django forms for adding Instagram accounts and subreddits.
"""
from django import forms
from .models import InstagramAccount, Subreddit


class InstagramAccountForm(forms.ModelForm):
    """
    Form for adding a new Instagram account to monitor.
    """
    username = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter Instagram username (without @)',
        }),
        help_text="Enter the Instagram username without the @ symbol"
    )

    class Meta:
        model = InstagramAccount
        fields = ['username']
        
    def clean_username(self):
        """
        Clean and validate username: remove @ if present, strip whitespace, convert to lowercase.
        """
        username = self.cleaned_data.get('username', '').strip()
        # Remove @ symbol if user included it
        username = username.lstrip('@').lower()
        if not username:
            raise forms.ValidationError("Username cannot be empty")
        return username


class SubredditForm(forms.ModelForm):
    """
    Form for adding a new subreddit to monitor.
    """
    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter subreddit name (without r/)',
        }),
        help_text="Enter the subreddit name without the r/ prefix"
    )

    class Meta:
        model = Subreddit
        fields = ['name']
        
    def clean_name(self):
        """
        Clean and validate subreddit name: remove r/ if present, strip whitespace.
        """
        name = self.cleaned_data.get('name', '').strip().lower()
        # Remove r/ prefix if user included it
        name = name.lstrip('r/')
        if not name:
            raise forms.ValidationError("Subreddit name cannot be empty")
        return name


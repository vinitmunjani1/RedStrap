"""
Django forms for adding Instagram accounts and subreddits.
"""
from django import forms
from .models import InstagramAccount, Subreddit, TwitterAccount


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


class SocialAccountForm(forms.Form):
    """
    Combined form to add Instagram and Twitter usernames in one action.
    Either field can be provided; at least one is required.
    """
    instagram_username = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Instagram username (without @)',
        }),
        help_text="Optional. Enter Instagram username without @"
    )
    twitter_username = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Twitter username (without @)',
        }),
        help_text="Optional. Enter Twitter username without @"
    )

    def clean(self):
        """
        Ensure at least one handle is provided; normalize both if present.
        """
        cleaned_data = super().clean()
        ig = (cleaned_data.get('instagram_username') or '').strip().lstrip('@')
        tw = (cleaned_data.get('twitter_username') or '').strip().lstrip('@')

        if not ig and not tw:
            raise forms.ValidationError("Provide at least one username (Instagram or Twitter).")

        cleaned_data['instagram_username'] = ig.lower() if ig else ''
        cleaned_data['twitter_username'] = tw
        return cleaned_data


class TwitterAccountForm(forms.ModelForm):
    """
    Form for adding a new Twitter account to monitor.
    """
    username = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter Twitter username (without @)',
        }),
        help_text="Enter the Twitter username without the @ symbol"
    )

    class Meta:
        model = TwitterAccount
        fields = ['username']
        
    def clean_username(self):
        """
        Clean and validate username: remove @ if present, strip whitespace.
        """
        username = self.cleaned_data.get('username', '').strip()
        # Remove @ symbol if user included it
        username = username.lstrip('@')
        if not username:
            raise forms.ValidationError("Username cannot be empty")
        return username


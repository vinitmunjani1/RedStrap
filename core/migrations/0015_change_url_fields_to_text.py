# Generated migration to change URL fields from VARCHAR(500) to TEXT

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_ideavideoprompt'),
    ]

    operations = [
        # Change InstagramPost image_url and video_url from URLField(max_length=500) to TextField
        migrations.AlterField(
            model_name='instagrampost',
            name='image_url',
            field=models.TextField(blank=True, help_text='URL to the post image (unlimited length)'),
        ),
        migrations.AlterField(
            model_name='instagrampost',
            name='video_url',
            field=models.TextField(blank=True, help_text='URL to the post video if it\'s a video (unlimited length)'),
        ),
        # Change InstagramCarouselItem image_url and video_url from URLField(max_length=500) to TextField
        migrations.AlterField(
            model_name='instagramcarouselitem',
            name='image_url',
            field=models.TextField(blank=True, help_text='URL to the image if this is an image (unlimited length)'),
        ),
        migrations.AlterField(
            model_name='instagramcarouselitem',
            name='video_url',
            field=models.TextField(blank=True, help_text='URL to the video if this is a video (unlimited length)'),
        ),
    ]


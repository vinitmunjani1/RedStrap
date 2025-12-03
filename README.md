# REDSTRAP - Instagram & Reddit Analytics Platform

A Django-based platform for monitoring and analyzing Instagram posts, reels, and Reddit content.

## Features

### Instagram
- **Post Scraping**: Fetch all posts or recent posts (last 48 hours) from Instagram accounts
- **Reels Support**: Dedicated reels fetching with accurate timestamp extraction
- **Analytics**: View engagement metrics, top posts, and account statistics
- **Smart Fetching**: Automatically fetches all posts on first run, then only recent posts
- **Concurrent Processing**: Uses multiple API keys for faster fetching (10 calls/sec total)

### Reddit
- **Subreddit Monitoring**: Add and monitor multiple subreddits
- **Post Scraping**: Scrape Reddit posts (coming soon)
- **Keyword Extraction**: Extract keywords from posts (coming soon)

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set up environment variables (create `.env` file):
```
DJANGO_SECRET_KEY=your-secret-key-here
RAPIDAPI_KEY=your-rapidapi-key-here  # Optional if using multiple keys in settings
```

3. Run migrations:
```bash
python manage.py migrate
```

4. Create superuser:
```bash
python manage.py createsuperuser
```

5. Run development server:
```bash
python manage.py runserver
```

## Configuration

### RapidAPI Keys
The application supports multiple RapidAPI keys for load balancing. Configure them in `redstrap_project/settings.py`:

```python
RAPIDAPI_KEYS = [
    'key1',
    'key2',
    'key3',
    'key4',
    'key5'
]
```

### Debug: Save API Responses
To save API JSON responses to files for debugging and analysis, enable the debug mode in `redstrap_project/settings.py`:

```python
# Enable saving API responses to files
DEBUG_SAVE_RESPONSES = True  # Set to True to enable
DEBUG_MAX_RESPONSE_FILES = 50  # Maximum files to keep per endpoint type
```

Or set via environment variable:
```bash
export DEBUG_SAVE_RESPONSES=true
export DEBUG_MAX_RESPONSE_FILES=50
```

When enabled, API responses will be saved to:
- `debug_responses/reels/<username>_<timestamp>.json` - Reels endpoint responses
- `debug_responses/posts/<username>_<timestamp>.json` - Posts endpoint responses
- `debug_responses/post_detail/<username>_<timestamp>.json` - Post detail responses

The system automatically cleans up old files, keeping only the most recent N files (default: 50) per endpoint type to prevent disk space issues.

## Usage

1. **Register/Login**: Create an account or login
2. **Add Instagram Accounts**: Add Instagram usernames to monitor
3. **Fetch Posts**: Click "Fetch Posts" to scrape posts (first time fetches all, subsequent times fetch last 48 hours)
4. **Fetch Reels**: Click "Fetch Reels" to scrape all reels for all accounts
5. **View Analytics**: View engagement metrics and top performing posts

## Management Commands

- `python manage.py scrape_instagram`: Scrape posts via command line
- `python manage.py fix_reel_timestamps`: Fix timestamps for existing reels
- `python manage.py delete_all_reels`: Delete all reels from database

## Project Structure

```
REDSTRAP/
├── core/                    # Main Django app
│   ├── models.py           # Database models
│   ├── views.py            # View functions
│   ├── services/           # Service layer
│   │   ├── instagram_service.py
│   │   ├── reddit_service.py
│   │   └── keyword_service.py
│   └── templates/          # HTML templates
├── redstrap_project/        # Django project settings
│   ├── settings.py
│   └── urls.py
└── manage.py
```

## Features in Detail

### Timestamp Extraction
- Uses `taken_at` from API when available
- Falls back to `caption.created_at` if `taken_at` is in the future
- Extracts timestamp from Instagram snowflake ID as last resort
- Comprehensive validation to prevent future dates

### Rate Limiting
- Sliding window rate limiting per API key
- Supports 2 calls/second per key
- With 5 keys: 10 calls/second total capacity
- Automatic retry with different keys on failure

## License

MIT


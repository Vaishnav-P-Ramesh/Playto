web: gunicorn payout_engine.wsgi:application --bind 0.0.0.0:$PORT
worker: celery -A payout_engine worker --loglevel=info
release: python manage.py migrate && python manage.py seed_demo_data

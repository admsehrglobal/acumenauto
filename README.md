# Acumen Auto

Downloads the DCI/Acumen Power BI exports and emails them to the ZipRide inbox.

## Running the tests

The fast suite needs no database, browser or email key:

```
poetry run python -m unittest discover -s tests -t .
```

The page tests in `app/tests.py` need the database, so they run through
Django's runner. `acumenauto.settings` reads its configuration from the
environment, so export dummies first (Git Bash shown):

```
export SECRET_KEY=test DATABASE_URL=sqlite:///:memory: BREVO_API_KEY=x \
  DEFAULT_FROM_EMAIL=test@example.invalid CELERY_BROKER_URL=memory:// \
  DCI_REPORT_URL=u DCI_REPORT_URL_2=u DCI_REPORT_URL_3=u \
  DCI_REPORT_BUTTON_NAME=R1 DCI_REPORT_BUTTON_NAME_2=R2 DCI_REPORT_BUTTON_NAME_3=R3
poetry run python manage.py test
```

That command runs everything, `tests/` included.

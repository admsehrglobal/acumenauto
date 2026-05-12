FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

WORKDIR /code

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

RUN playwright install chromium --with-deps

COPY . .

# collectstatic en build: empaqueta el admin de Django + cualquier static futuro.
# SECRET_KEY dummy solo para que settings importe — no se persiste.
RUN SECRET_KEY=build-only \
    DCI_USERNAME=x DCI_PASSWORD=x DCI_REPORT_URL=x DCI_REPORT_BUTTON_NAME=x \
    DCI_REPORT_URL_2=x DCI_REPORT_BUTTON_NAME_2=x \
    DCI_REPORT_URL_3=x DCI_REPORT_BUTTON_NAME_3=x \
    BREVO_API_KEY=x DEFAULT_FROM_EMAIL=x \
    CELERY_BROKER_URL=redis://localhost \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Sin CMD — fly.toml [processes] define cada uno (web/worker/beat).
# Para correr local con docker-compose, los `command:` del compose lo definen.

# One image for both services (dashboard and scheduler); docker-compose.yml chooses the command.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    ANONYMIZED_TELEMETRY=False \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Dependencies first so code changes do not re-download them.
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY . .

# Run as a non-root user. /data holds the database, vector store and evaluation log (mount a volume here).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app
VOLUME /data

EXPOSE 8501

# Default command is the dashboard; docker-compose.yml overrides it for the scheduler.
CMD ["streamlit", "run", "app.py"]

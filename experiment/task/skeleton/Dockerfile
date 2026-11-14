# FIXED SKELETON — the app image.
#
# The app's image. The SERVED HTTP API is the same however the app is run,
# which is what keeps the end-to-end checks and the load test identical
# across arms.
FROM python:3.11-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080
# SERVICE_PORT/TASK/DB_DSN/CACHE_* are injected by the deployment env.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

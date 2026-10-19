FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser app /app/app
COPY --chown=appuser:appuser config /app/config
COPY --chown=appuser:appuser docker/launch.py /app/docker/launch.py
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser
EXPOSE 8765
ENTRYPOINT ["python", "/app/docker/launch.py"]
CMD ["api"]

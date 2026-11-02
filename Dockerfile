FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 qinlin \
    && useradd --uid 10001 --gid 10001 --no-create-home qinlin \
    && mkdir -p /app/data && chown 10001:10001 /app/data
COPY --chown=10001:10001 app.py qinlin.py storage.py manage.py ./
COPY --chown=10001:10001 static ./static
COPY THIRD_PARTY_NOTICES.md ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]

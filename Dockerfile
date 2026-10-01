FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Stateless: no volume, no database. The XSDs in schemas/ ship in the image; if they are
# missing the app refuses to serve downloads rather than hand out unvalidated files.
RUN python tools/fetch_schemas.py

EXPOSE 8080

# waitress, not gunicorn, so the container runs the same server as the Windows host.
CMD ["waitress-serve", "--listen=0.0.0.0:8080", "app:app"]

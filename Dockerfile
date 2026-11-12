# The team server (and the controller, which is the same image started with another command).
#
#   docker build -t ghcr.io/<you>/recurser:0.2.0 .
#
# The decks under data/ are copied into the image: build from the checkout that holds the decks this
# deployment may serve, and push the image to a registry as private as those decks.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY recurser ./recurser
COPY app ./app
COPY data ./data

# No shell account, no home, nothing writable but the history volume and /tmp.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin recurser \
    && mkdir -p /var/lib/recurser /app/config /app/secrets \
    && chown 10001:10001 /var/lib/recurser
USER 10001

# Layer 1: an API service on port 8000.  In Kubernetes the settings arrive at /app/config/config.yaml
# and the Deployment passes its own arguments; this default is for `docker run`.
EXPOSE 8000
ENTRYPOINT ["python", "-m", "recurser"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]

# Smart AI Gateway — image de production (7.4)
# Une seule image pour la gateway (défaut) et pour l'agent de pont :
#   docker run … smart-ai-gateway python -m gateway.bridge_agent
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GATEWAY_DB=/data/gateway.db \
    GATEWAY_ENCRYPTION_KEY_FILE=/data/encryption.key \
    GATEWAY_PORT=8000
# clé générée sans GATEWAY_ENCRYPTION_KEY : dans le volume, sinon /app/data (non accessible en écriture) fait
# échouer le démarrage, et une clé recréée avec le conteneur rendrait illisibles les identifiants chiffrés (15.2)

WORKDIR /app
# dépendances d'exécution seulement (les outils de test sont dans requirements-dev.txt)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gateway ./gateway

# utilisateur non root ; la base SQLite vit dans un volume
RUN useradd --uid 10001 --create-home gateway \
    && mkdir -p /data && chown gateway:gateway /data
USER gateway
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('GATEWAY_PORT','8000'), timeout=4)" || exit 1

# --proxy-headers : derrière Caddy, l'IP client et le schéma https sont ceux du visiteur
# (FORWARDED_ALLOW_IPS limite les proxys de confiance ; voir docker-compose.cloud.yml)
CMD ["sh", "-c", "exec uvicorn gateway.main:app --host 0.0.0.0 --port ${GATEWAY_PORT} --proxy-headers --ws websockets-sansio --ws-max-size 67108864 --timeout-graceful-shutdown 20"]

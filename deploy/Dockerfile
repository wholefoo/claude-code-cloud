# RedBlue platform image. Build from the repository root:
#   docker build -f deploy/Dockerfile -t redblue .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 redblue
WORKDIR /opt/redblue
COPY packages ./packages
COPY admin ./admin
RUN pip install ./packages/core[postgres,ai] ./packages/cms ./packages/templates \
      ./packages/growth ./packages/observe ./packages/gate ./packages/builder ./admin \
      ./packages/platform ./packages/video ./packages/cli
WORKDIR /srv/site
RUN mkdir -p /srv/site/media && chown -R redblue:redblue /srv/site
USER redblue
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/_rb/health')"
CMD ["uvicorn", "redblue.platform.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

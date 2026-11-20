# ScoutOSS notebooks: JupyterHub (Keycloak OIDC login) with pyxnat and the data-lake clients.
FROM quay.io/jupyterhub/jupyterhub:5.4.4
# Pick up Ubuntu security fixes released after the base image (CI Trivy gate).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir \
      jupyterlab==4.5.11 \
      oauthenticator==17.4.0 \
      pyxnat==1.6.3 \
      opensearch-py==3.0.0 \
      pandas==2.3.1 \
      matplotlib==3.10.3 \
      pydicom==3.0.2 \
      httpx==0.28.1 \
      certifi==2026.10.18 \
      cryptography==50.0.0 \
      oauthlib==4.0.1 \
      Mako==1.3.12 \
      urllib3==2.8.0 \
 && useradd -m -u 10001 -s /bin/bash jovyan \
 && mkdir -p /srv/jupyterhub /home/jovyan/examples \
 && chown -R 10001:10001 /srv/jupyterhub /home/jovyan
COPY jupyterhub_config.py /srv/jupyterhub/jupyterhub_config.py
COPY --chown=10001:10001 examples/ /home/jovyan/examples/
USER 10001
WORKDIR /srv/jupyterhub
EXPOSE 8000
CMD ["jupyterhub", "-f", "/srv/jupyterhub/jupyterhub_config.py"]

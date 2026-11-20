# ScoutOSS notebooks: JupyterHub (Keycloak OIDC login) with pyxnat and the data-lake clients.
FROM quay.io/jupyterhub/jupyterhub:5.4.4
RUN python3 -m pip install --no-cache-dir \
      jupyterlab==4.4.4 \
      oauthenticator==17.3.0 \
      pyxnat==1.6.3 \
      opensearch-py==3.0.0 \
      pandas==2.3.1 \
      matplotlib==3.10.3 \
      pydicom==3.0.2 \
      httpx==0.28.1 \
 && useradd -m -u 10001 -s /bin/bash jovyan \
 && mkdir -p /srv/jupyterhub /home/jovyan/examples \
 && chown -R 10001:10001 /srv/jupyterhub /home/jovyan
COPY jupyterhub_config.py /srv/jupyterhub/jupyterhub_config.py
COPY --chown=10001:10001 examples/ /home/jovyan/examples/
USER 10001
WORKDIR /srv/jupyterhub
EXPOSE 8000
CMD ["jupyterhub", "-f", "/srv/jupyterhub/jupyterhub_config.py"]

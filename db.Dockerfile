# Image Postgres avec les scripts d'init EMBARQUÉS (bind-mount interdit sur ce Portainer).
# Les fichiers de initdb/ sont exécutés une seule fois, au tout premier démarrage (base vide),
# dans l'ordre alphabétique — comportement standard de l'entrypoint postgres.
FROM postgres:16-alpine
COPY initdb/ /docker-entrypoint-initdb.d/

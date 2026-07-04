#!/bin/sh
set -eu

# Refresh runtime images first, then rebuild the Django image layer stack with
# the current workspace contents. The final up step force-recreates containers
# so bind-mounted frontend code and rebuilt app images are both applied.
docker compose pull --ignore-pull-failures db redis elasticsearch qdrant frontend nginx
docker compose build --pull app migrate celery-worker celery-beat
docker compose --compatibility up -d --build --force-recreate --remove-orphans --wait \
	app migrate celery-worker celery-beat frontend nginx db redis elasticsearch qdrant

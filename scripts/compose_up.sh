#!/bin/sh
set -eu

# Refresh runtime images first, then rebuild the Go image layer stack with
# the current workspace contents. The final up step force-recreates containers
# so bind-mounted frontend code and rebuilt app images are both applied.
docker compose pull --ignore-pull-failures db redis frontend nginx
docker compose build --pull app migrate worker browser
docker compose --compatibility up -d --build --force-recreate --remove-orphans --wait \
	app migrate worker browser frontend nginx db redis

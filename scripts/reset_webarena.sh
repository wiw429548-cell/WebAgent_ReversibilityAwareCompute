#!/usr/bin/env bash
# Restore the given sites (default: all four) to their initial state by re-creating the containers from the images.
source "$(dirname "$0")/webarena_common.sh"
SITES=${@:-shopping shopping_admin reddit gitlab}
rc=0
for s in $SITES; do
  $CLI rm -f "$(container_name $s)" >/dev/null 2>&1
  start_site $s
done
for s in $SITES; do wait_site $s || rc=1; done
exit $rc

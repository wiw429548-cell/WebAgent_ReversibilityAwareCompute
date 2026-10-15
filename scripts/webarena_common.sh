#!/usr/bin/env bash
# shared helpers for setup / reset of the four WebArena sites (shopping, shopping_admin, reddit, gitlab)
# WA_CLI          : container CLI, docker (default) or podman (rootless podman is CLI-compatible)
# WA_HOSTNAME     : host name the browser uses to reach the sites (must match the WA_* URLs), default localhost
# WA_IMAGE_SOURCE : verified (default) = ServiceNow WebArena-Verified images from Docker Hub (am1n3e/webarena-verified-
#                   <site>:WA_VERIFIED_TAG; same site data and DB credentials as the originals + upstream bug fixes,
#                   ~33 GB compressed, no download quota; pre-downloaded copies in downloads/docker_images/<site>)
#                   original = the original WebArena tarballs from Google Drive (~208 GB, daily download quota)
# WA_IMG_DIR      : where the ORIGINAL tarballs are downloaded (default: <project>/webarena_images)
# Container names are the same for both sources (shopping, shopping_admin, forum, gitlab): the DB side-effect probes
# (race/env.py) and the resets work unchanged.
CLI=${WA_CLI:-docker}
HOST=${WA_HOSTNAME:-localhost}
PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMG_DIR=${WA_IMG_DIR:-$PROJ/webarena_images}
SRC=${WA_IMAGE_SOURCE:-verified}
VTAG=${WA_VERIFIED_TAG:-0.1.0}
DL_DIR=${WA_DOCKER_DL_DIR:-$PROJ/downloads/docker_images}

image_of() {
  if [ "$SRC" = verified ]; then echo "am1n3e/webarena-verified-$1:$VTAG"; return; fi
  case "$1" in
    shopping) echo shopping_final_0712 ;;
    shopping_admin) echo shopping_admin_final_0719 ;;
    reddit) echo postmill-populated-exposed-withimg ;;
    gitlab) echo gitlab-populated-final-port8023 ;;
  esac
}

container_name() { if [ "$1" = reddit ]; then echo forum; else echo "$1"; fi; }

retry_exec() {  # container, tries, command... : services inside a fresh container need a while to come up
  local c=$1 n=$2 i; shift 2
  for i in $(seq 1 "$n"); do
    if $CLI exec "$c" "$@"; then return 0; fi
    sleep 15
  done
  echo "command failed in $c after $n tries: $*"; return 1
}

start_site_verified() {
  local img; img=$(image_of "$1")
  case "$1" in
    shopping)
      $CLI run --name shopping -p 7770:80 -d "$img"
      retry_exec shopping 40 env-ctrl init --base-url "http://$HOST:7770/"
      # long sessions: an episode (incl. slow LLM steps) must never be logged out mid-task
      $CLI exec shopping php /var/www/magento2/bin/magento config:set web/cookie/cookie_lifetime 86400 || true
      $CLI exec shopping php /var/www/magento2/bin/magento cache:flush || true ;;
    shopping_admin)
      $CLI run --name shopping_admin -p 7780:80 -d "$img"
      retry_exec shopping_admin 40 env-ctrl init --base-url "http://$HOST:7780/"
      for kv in "admin/security/password_is_forced 0" "admin/security/password_lifetime 0" \
                "admin/security/session_lifetime 86400"; do
        $CLI exec shopping_admin php /var/www/magento2/bin/magento config:set $kv || true
      done
      $CLI exec shopping_admin php /var/www/magento2/bin/magento cache:flush || true ;;
    reddit)
      $CLI run --name forum -p 9999:80 -d "$img"
      retry_exec forum 40 env-ctrl init --base-url "http://$HOST:9999/" ;;
    gitlab)
      # GitLab (puma/prometheus) needs more than the 64 MB default /dev/shm, otherwise puma keeps restarting (502)
      # IPv4-only publish: GitLab's nginx does not listen on IPv6, so a rootless-podman forward of [::1]:8023 accepts
      # and then RESETS the connection (curl 000, Chromium ERR_CONNECTION_RESET) because `localhost` resolves to ::1
      # first. Bound to 127.0.0.1 only, ::1 is refused and clients fall back to 127.0.0.1.
      $CLI run --name gitlab --shm-size 256m -p 127.0.0.1:8023:8023 -d "$img"
      retry_exec gitlab 60 env-ctrl init --base-url "http://$HOST:8023" ;;
  esac
}

start_site_original() {
  case "$1" in
    shopping)
      $CLI run --name shopping -p 7770:80 -d shopping_final_0712
      sleep 60
      $CLI exec shopping /var/www/magento2/bin/magento setup:store-config:set --base-url="http://$HOST:7770"
      $CLI exec shopping mysql -u magentouser -pMyPassword magentodb -e "UPDATE core_config_data SET value='http://$HOST:7770/' WHERE path = 'web/secure/base_url';"
      # long sessions: an episode (incl. slow LLM steps) must never be logged out mid-task
      $CLI exec shopping /var/www/magento2/bin/magento config:set web/cookie/cookie_lifetime 86400 || true
      $CLI exec shopping /var/www/magento2/bin/magento cache:flush ;;
    shopping_admin)
      $CLI run --name shopping_admin -p 7780:80 -d shopping_admin_final_0719
      sleep 60
      $CLI exec shopping_admin php /var/www/magento2/bin/magento config:set admin/security/password_is_forced 0
      $CLI exec shopping_admin php /var/www/magento2/bin/magento config:set admin/security/password_lifetime 0
      $CLI exec shopping_admin php /var/www/magento2/bin/magento config:set admin/security/session_lifetime 86400 || true
      $CLI exec shopping_admin /var/www/magento2/bin/magento setup:store-config:set --base-url="http://$HOST:7780"
      $CLI exec shopping_admin mysql -u magentouser -pMyPassword magentodb -e "UPDATE core_config_data SET value='http://$HOST:7780/' WHERE path = 'web/secure/base_url';"
      $CLI exec shopping_admin /var/www/magento2/bin/magento cache:flush ;;
    reddit)
      $CLI run --name forum -p 9999:80 -d postmill-populated-exposed-withimg ;;
    gitlab)
      $CLI run --name gitlab -d -p 8023:8023 gitlab-populated-final-port8023 /opt/gitlab/embedded/bin/runsvdir-start
      sleep 120
      $CLI exec gitlab update-permissions || true
      $CLI exec gitlab sed -i "s|^external_url.*|external_url 'http://$HOST:8023'|" /etc/gitlab/gitlab.rb
      $CLI exec gitlab gitlab-ctl reconfigure ;;
  esac
}

start_site() { if [ "$SRC" = verified ]; then start_site_verified "$1"; else start_site_original "$1"; fi; }

load_verified() {  # site -> 0 when the image is in the container runtime (local copy loaded, else pulled)
  local s=$1 img d files; img=$(image_of "$s"); d="$DL_DIR/$s"
  if $CLI image inspect "$img" >/dev/null 2>&1; then echo "image $img present"; return 0; fi
  if [ -f "$d/COMPLETE" ]; then
    echo "loading $img from $d"
    files=$(cd "$d" && ls | grep -v -e '^COMPLETE$' -e '\.part$')
    # stream the unpacked docker-save archive (no second copy on disk)
    (cd "$d" && tar -cf - $files) | $CLI load && $CLI image inspect "$img" >/dev/null 2>&1 && return 0
    echo "loading $d failed -> pulling from Docker Hub"
  fi
  $CLI pull "$img"
}

wait_http() {  # url, timeout seconds, [container] -> diagnostics on timeout
  local t=0 code
  until code=$(curl -s -o /dev/null -w "%{http_code}" "$1"); echo "$code" | grep -qE "200|302"; do
    if [ -n "$3" ] && [ $((t % 300)) -eq 0 ] && [ $t -gt 0 ]; then echo "  waiting for $1 (${t}s, http $code)"; fi
    sleep 10; t=$((t+10))
    if [ $t -ge ${2:-900} ]; then
      echo "timeout waiting for $1 (last http code $code)"
      if [ -n "$3" ]; then
        echo "---- diagnostics for container $3 ----"
        $CLI ps -a --filter "name=$3"
        $CLI exec "$3" sh -c 'gitlab-ctl status 2>/dev/null; df -h /dev/shm; free -m' 2>&1 | tail -30
        $CLI logs --tail 60 "$3" 2>&1
        echo "---- end diagnostics ----"
      fi
      return 1
    fi
  done
  echo "up: $1"
}

wait_site() {
  case "$1" in
    shopping) wait_http "http://$HOST:7770" 900 shopping ;;
    shopping_admin) wait_http "http://$HOST:7780/admin" 900 shopping_admin ;;
    reddit) wait_http "http://$HOST:9999" 900 forum ;;
    gitlab) wait_http "http://$HOST:8023" 3600 gitlab ;;
  esac
}

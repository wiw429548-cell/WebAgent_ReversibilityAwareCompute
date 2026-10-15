#!/usr/bin/env bash
# One-time deployment of the WebArena sites used by WebArena-Lite (shopping, shopping_admin, reddit, gitlab).
# Needs docker or rootless podman (WA_CLI) and >=32 GB RAM. Official WebArena image tarballs:
#   shopping 67.6 GB, shopping_admin 9.6 GB, forum 53.4 GB, gitlab 77.8 GB  (~208 GB in total)
# Each tarball is downloaded to WA_IMG_DIR, loaded into the container runtime and then deleted (WA_KEEP_TARS=1 keeps
# it), so WA_IMG_DIR needs ~80 GB free and the container storage ~210 GB.
#
# Sources (state Oct 2026): Google Drive (official mirror; works, but each file has a daily anonymous download quota),
# archive.org (items taken down, "dark") and the CMU server (403). Downloads use BOUNDED byte ranges appended to the
# tarball - Google Drive answers open-ended ranges (what wget -c / curl -C - send) and quota-exceeded requests with a
# small HTML page, so every chunk is length-checked before it is appended. When the quota is exhausted the script
# waits (15 min steps, up to WA_DL_MAX_HOURS, default 24) and resumes; it is safe to interrupt and re-run.
# A complete tarball placed in WA_IMG_DIR by hand (e.g. downloaded with rclone and your own Google account, see
# RUN.md) is used as is. WA_DOWNLOAD_ONLY=1 (run_all.py --download_only) only downloads the tarballs (no container
# runtime needed) and keeps them for a later load. The map (OSM) and wikipedia (kiwix) sites are not deployed (their tasks are skipped).
#
# DEFAULT (WA_IMAGE_SOURCE=verified): the ServiceNow WebArena-Verified images (Docker Hub, ~33 GB compressed, no
# quota). A copy pre-downloaded with `python scripts/fetch_images.py` (downloads/docker_images/<site>) is loaded from
# disk; otherwise the image is pulled. WA_DOWNLOAD_ONLY=1 runs the docker-free downloader only.
# WA_IMAGE_SOURCE=original: the Google Drive tarballs described above.
set -e
source "$(dirname "$0")/webarena_common.sh"
if [ "$SRC" = verified ]; then
  for s in shopping shopping_admin reddit gitlab; do
    if [ -n "$WA_ONLY" ] && [[ " $WA_ONLY " != *" $s "* ]]; then continue; fi
    if [ -n "$WA_DOWNLOAD_ONLY" ]; then
      "${PYTHON:-python3}" "$PROJ/scripts/fetch_images.py" "$s"
    else
      load_verified "$s"
    fi
  done
  if [ -n "$WA_DOWNLOAD_ONLY" ]; then echo "WebArena-Verified images are in $DL_DIR"; exit 0; fi
  for s in shopping shopping_admin reddit gitlab; do
    if $CLI inspect "$(container_name $s)" >/dev/null 2>&1; then
      $CLI start "$(container_name $s)" >/dev/null 2>&1 || true
    else
      start_site $s
    fi
  done
  for s in shopping shopping_admin reddit gitlab; do wait_site $s; done
  echo "WebArena sites (WebArena-Verified $VTAG) ready on $HOST"
  exit 0
fi
mkdir -p "$IMG_DIR"
declare -A ITEM=( [shopping_final_0712]=webarena-env-shopping-image [shopping_admin_final_0719]=webarena-env-shopping-admin-image
                  [postmill-populated-exposed-withimg]=webarena-env-forum-image [gitlab-populated-final-port8023]=webarena-env-gitlab-image )
declare -A GDRIVE=( [shopping_final_0712]=1gxXalk9O0p9eu1YkIJcmZta1nvvyAJpA [shopping_admin_final_0719]=1See0ZhJRw0WTTL9y8hFlgaduwPZ_nGfd
                    [postmill-populated-exposed-withimg]=17Qpp1iu_mPqzgO_73Z9BnFjHrzmX9DGf [gitlab-populated-final-port8023]=19W8qM0DPyRvWCLyQe0qtnCWAHGruolMR )
declare -A SIZE=( [shopping_final_0712]=67575898112 [shopping_admin_final_0719]=9640032256
                  [postmill-populated-exposed-withimg]=53435097088 [gitlab-populated-final-port8023]=77755595776 )
MAX_WAIT=$(( ${WA_DL_MAX_HOURS:-24} * 3600 ))

fsize() { stat -c %s "$1" 2>/dev/null || echo 0; }

probe() {  # url size -> 0 if the url serves the binary file of that size (checks the first 1 KB only)
  local h
  h=$(curl -sL -r 0-1023 -o /dev/null -D - --max-time 120 "$1" 2>/dev/null | tr -d '\r') || return 1
  echo "$h" | grep -qi "^content-range: bytes 0-1023/$2"
}

fetch() {  # url file size -> 0 when the file is complete
  local url=$1 f=$2 size=$3 have end chunk=$((512 * 1024 * 1024)) part="$2.part" t0 n=0 tries why w
  have=$(fsize "$f")
  if [ "$have" -gt "$size" ]; then echo "  $f is larger than expected -> starting over"; rm -f "$f"; have=0; fi
  echo "  source ${url%%\?*}  ($((have / 1073741824)) of $((size / 1073741824)) GB present)"
  t0=$(date +%s)
  while [ "$have" -lt "$size" ]; do
    end=$((have + chunk - 1))
    [ "$end" -ge "$size" ] && end=$((size - 1))
    tries=0
    while :; do
      if curl -sfL --connect-timeout 60 --max-time 3600 -r "$have-$end" -o "$part" "$url" && \
         [ "$(fsize "$part")" -eq $((end - have + 1)) ]; then
        break
      fi
      tries=$((tries + 1))
      why="request failed"
      if grep -qi "quota exceeded" "$part" 2>/dev/null; then why="Google Drive download quota of this file exhausted"; fi
      rm -f "$part"
      if [ $(( $(date +%s) - t0 )) -gt "$MAX_WAIT" ]; then echo "  giving up after ${WA_DL_MAX_HOURS:-24} h ($why)"; return 1; fi
      w=900
      [ "$tries" -le 3 ] && w=30
      echo "  $(date '+%m-%d %H:%M') at $((have / 1073741824)) GB: $why -> waiting $((w / 60)) min, then resuming"
      sleep $w
    done
    cat "$part" >> "$f"
    rm -f "$part"
    have=$((end + 1))
    n=$((n + 1))
    if [ $((n % 10)) = 0 ]; then echo "  $(date '+%m-%d %H:%M') $((have / 1073741824)) / $((size / 1073741824)) GB"; fi
  done
  [ "$(fsize "$f")" -eq "$size" ]
}

for img in "${!ITEM[@]}"; do
  if [ -n "$WA_ONLY" ] && [[ " $WA_ONLY " != *" $img "* ]]; then continue; fi  # WA_ONLY: subset (parallel downloads)
  if $CLI image inspect "$img" >/dev/null 2>&1; then echo "image $img present"; continue; fi
  f="$IMG_DIR/$img.tar"
  echo "image $img ($((SIZE[$img] / 1073741824)) GB) -> $f"
  ok=0
  if [ "$(fsize "$f")" -eq "${SIZE[$img]}" ] && tar -tf "$f" >/dev/null 2>&1; then ok=1; echo "  complete tarball already present"; fi
  if [ $ok != 1 ]; then
    for url in "https://archive.org/download/${ITEM[$img]}/$img.tar" \
               "http://metis.lti.cs.cmu.edu/webarena-images/$img.tar"; do  # old mirrors: only if they come back
      if probe "$url" "${SIZE[$img]}" && fetch "$url" "$f" "${SIZE[$img]}"; then ok=1; break; fi
    done
  fi
  if [ $ok != 1 ] && fetch "https://drive.usercontent.google.com/download?id=${GDRIVE[$img]}&export=download&confirm=t" \
                           "$f" "${SIZE[$img]}"; then
    ok=1
  fi
  if [ $ok != 1 ]; then
    echo "download of $img failed. Download it manually with your own Google account (RUN.md: rclone), e.g."
    echo "  https://drive.google.com/file/d/${GDRIVE[$img]}/view   ->  $f"
    echo "then re-run (a complete $f is used as is)."
    exit 1
  fi
  if [ -n "$WA_DOWNLOAD_ONLY" ]; then echo "  downloaded $f (WA_DOWNLOAD_ONLY: not loaded)"; continue; fi
  $CLI load --input "$f"
  if [ -z "$WA_KEEP_TARS" ] && $CLI image inspect "$img" >/dev/null 2>&1; then rm -f "$f"; echo "  loaded; removed $f"; fi
done
if [ -n "$WA_DOWNLOAD_ONLY" ]; then echo "all WebArena image tarballs are in $IMG_DIR"; exit 0; fi
for s in shopping shopping_admin reddit gitlab; do
  name=$(container_name $s)
  if $CLI inspect "$name" >/dev/null 2>&1; then
    $CLI start "$name" >/dev/null 2>&1 || true
  else
    start_site $s
  fi
done
for s in shopping shopping_admin reddit gitlab; do wait_site $s; done
echo "WebArena sites ready on $HOST"

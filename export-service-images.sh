#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

SERVICE_IMAGES="${SERVICE_IMAGES:-oracle-recovery-service-api:latest oracle-recovery-service-worker:latest}"
SERVICE_IMAGE_TAR="${SERVICE_IMAGE_TAR:-oracle-recovery-service-images.tar.gz}"

for image in $SERVICE_IMAGES; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "Required service image was not found: $image" >&2
    exit 1
  fi
done

case "$SERVICE_IMAGE_TAR" in
  *.gz)
    echo "Exporting compressed service images to $SERVICE_IMAGE_TAR..."
    docker save $SERVICE_IMAGES | gzip -1 > "$SERVICE_IMAGE_TAR"
    ;;
  *)
    echo "Exporting service images to $SERVICE_IMAGE_TAR..."
    docker save -o "$SERVICE_IMAGE_TAR" $SERVICE_IMAGES
    ;;
esac

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$SERVICE_IMAGE_TAR" > "${SERVICE_IMAGE_TAR}.sha256"
  cat "${SERVICE_IMAGE_TAR}.sha256"
fi

echo "Done: $SERVICE_IMAGE_TAR"

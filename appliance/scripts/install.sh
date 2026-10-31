#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)

if [[ $EUID -ne 0 ]]; then
  printf 'Reco Field install: run with sudo\n' >&2
  exit 1
fi
if [[ $(uname -s) != Linux ]]; then
  printf 'Reco Field install: Linux is required\n' >&2
  exit 1
fi

# Run this before apt can alter the host and before any Reco Field service is
# stopped. Existing unrelated listeners are never touched.
python3 "$SCRIPT_DIR/deploy.py" precheck --source-root "$SOURCE_ROOT"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential ca-certificates clang cmake curl ffmpeg git iproute2 network-manager dnsmasq-base \
  pkg-config python3 python3-pip python3-venv util-linux \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  libvulkan-dev vulkan-tools libopencv-dev python3-opencv

python3 "$SCRIPT_DIR/deploy.py" install --source-root "$SOURCE_ROOT"

#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

systemctl disable --now reco-field.service reco-field-relay.service reco-field-network.service 2>/dev/null || true
rm -f /etc/systemd/system/reco-field.service /etc/systemd/system/reco-field-relay.service /etc/systemd/system/reco-field-network.service
systemctl daemon-reload
rm -rf /opt/reco-field
echo "Reco Field removed. Configuration, snapshots and recordings remain on the SSD. NetworkManager profiles and /etc/reco-field remain."
echo "The recofield service user and /etc/reco-field/mediamtx.yml were also preserved."

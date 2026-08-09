#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Uruchom jako root lub przez sudo: sudo $0"
  exit 1
fi

SHARE_DIR="/srv/autocore_photos"
SAMBA_CONF="/etc/samba/smb.conf"
SHARE_NAME="autocore_photos"

echo "Tworzę katalog współdzielony: $SHARE_DIR"
mkdir -p "$SHARE_DIR"
chown nobody:nogroup "$SHARE_DIR"
chmod 2775 "$SHARE_DIR"

if ! grep -q "^\[$SHARE_NAME\]" "$SAMBA_CONF" 2>/dev/null; then
  cat <<EOF >> "$SAMBA_CONF"
[$SHARE_NAME]
  path = $SHARE_DIR
  browsable = yes
  guest ok = yes
  read only = no
  create mask = 0664
  directory mask = 2775
  force user = nobody
  force group = nogroup
  guest account = nobody
EOF
  echo "Dodano udział Samba [$SHARE_NAME] do $SAMBA_CONF"
else
  echo "Udział Samba [$SHARE_NAME] już istnieje w $SAMBA_CONF"
fi

echo "Restartuję usługę Samba..."
systemctl restart smbd

echo ""
echo "Gotowe. Katalog zdjęć współdzielony jest jako \\SERVER\$SHARE_NAME lub smb://SERVER/$SHARE_NAME"
echo "Jeżeli program ma zapisywać zdjęcia w tym udziale, ustaw w programie katalog bazowy na $SHARE_DIR lub stwórz symlink do niego."

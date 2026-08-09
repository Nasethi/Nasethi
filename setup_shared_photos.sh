#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Uruchom jako root lub przez sudo: sudo $0"
  exit 1
fi

SMB_USER="${1:-autocore}"
SMB_PASS="${2:-autocore123}"
SHARE_DIR="/srv/autocore_photos"
SAMBA_CONF="/etc/samba/smb.conf"
SHARE_NAME="autocore_photos"

echo "Używam użytkownika Samba: $SMB_USER"
echo "Hasło Samba: $SMB_PASS"

echo "Tworzę katalog współdzielony: $SHARE_DIR"
mkdir -p "$SHARE_DIR"
chown nobody:nogroup "$SHARE_DIR"
chmod 2775 "$SHARE_DIR"

if ! id -u "$SMB_USER" >/dev/null 2>&1; then
  echo "Tworzę systemowego użytkownika: $SMB_USER"
  useradd -M -s /usr/sbin/nologin "$SMB_USER"
fi

if ! command -v smbpasswd >/dev/null 2>&1; then
  echo "smbpasswd nie jest dostępny. Zainstaluj samba-common-bin." >&2
  exit 1
fi

echo -ne "$SMB_PASS\n$SMB_PASS\n" | smbpasswd -s -a "$SMB_USER"

echo "Konfiguruję udział Samba..."
if ! grep -q "^\[$SHARE_NAME\]" "$SAMBA_CONF" 2>/dev/null; then
  cat <<EOF >> "$SAMBA_CONF"
[$SHARE_NAME]
  path = $SHARE_DIR
  browsable = yes
  read only = no
  valid users = $SMB_USER
  write list = $SMB_USER
  create mask = 0664
  directory mask = 2775
  force user = nobody
  force group = nogroup
  guest ok = no
EOF
  echo "Dodano udział Samba [$SHARE_NAME] do $SAMBA_CONF"
else
  echo "Udział Samba [$SHARE_NAME] już istnieje w $SAMBA_CONF"
  echo "Upewnij się, że w sekcji [$SHARE_NAME] masz poprawne ustawienia valid users i guest ok = no."
fi

echo "Restartuję usługę Samba..."
systemctl restart smbd

echo ""
echo "Gotowe. Katalog zdjęć współdzielony jest jako \\SERVER\$SHARE_NAME lub smb://SERVER/$SHARE_NAME"
echo "Użyj loginu: $SMB_USER i hasła: $SMB_PASS"
echo "Jeżeli program ma zapisywać zdjęcia w tym udziale, ustaw w programie katalog bazowy na $SHARE_DIR lub stwórz symlink do niego."

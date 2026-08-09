#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Uruchom jako root lub przez sudo: sudo $0"
  exit 1
fi

echo "Aktualizuję listę pakietów..."
apt update

PACKAGES=(
  postgresql
  postgresql-contrib
  samba
  samba-common-bin
  python3
  python3-pip
  python3-psycopg2
  python3-venv
  python3-tk
  python3-requests
  python3-bs4
)

echo "Instaluję pakiety systemowe..."
apt install -y "${PACKAGES[@]}"

echo "Instaluję pakiety Python z repozytorium Ubuntu..."
# W Ubuntu lepiej używać pakietów systemowych, aby uniknąć problemów z zarządzaniem środowiskiem.
# Pip jest dostępny wyłącznie w wirtualnych środowiskach lub jako pakiet użytkownika.

SHARE_DIR="/srv/autocore_photos"
SAMBA_CONF="/etc/samba/smb.conf"

echo "Tworzę katalog współdzielony dla zdjęć: $SHARE_DIR"
mkdir -p "$SHARE_DIR"
chown nobody:nogroup "$SHARE_DIR"
chmod 2775 "$SHARE_DIR"

if ! grep -q "^\[autocore_photos\]" "$SAMBA_CONF"; then
  cat <<'EOF' >> "$SAMBA_CONF"
[autocore_photos]
  path = /srv/autocore_photos
  browsable = yes
  guest ok = yes
  read only = no
  create mask = 0664
  directory mask = 2775
  force user = nobody
  force group = nogroup
  valid users = @users
  write list = @users
  guest account = nobody
EOF
  echo "Dodano udział Samba [autocore_photos] do $SAMBA_CONF"
else
  echo "Udział Samba [autocore_photos] już istnieje w $SAMBA_CONF"
fi

echo "Restartuję usługę Samba..."
systemctl restart smbd

echo "Instalacja zakończona."
echo "Dodatkowe kroki:"
echo "  1. Załaduj bazę PostgreSQL na KS i stwórz bazę autocore."
echo "  2. Skonfiguruj PostgreSQL w /etc/postgresql/*/main/postgresql.conf i pg_hba.conf."
echo "  3. Na klientach zamontuj udział sieciowy lub zrób symlink \"photos\" do "/srv/autocore_photos"."
echo "  4. W aplikacji ustaw PG_HOST na adres IP komputera KS."

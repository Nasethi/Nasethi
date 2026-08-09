#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Uruchom jako root lub przez sudo: sudo $0"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

echo "Aktualizuję pakiety..."
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

SHARE_DIR="/srv/autocore_photos"
SAMBA_CONF="/etc/samba/smb.conf"

if [ ! -d "$SHARE_DIR" ]; then
  echo "Tworzę katalog zdjęć: $SHARE_DIR"
  mkdir -p "$SHARE_DIR"
  chown nobody:nogroup "$SHARE_DIR"
  chmod 2775 "$SHARE_DIR"
fi

if ! grep -q "^\[autocore_photos\]" "$SAMBA_CONF" 2>/dev/null; then
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
  echo "Dodano udział Samba [autocore_photos]"
else
  echo "Udział Samba [autocore_photos] już istnieje"
fi

echo "Restartuję Sambę..."
systemctl restart smbd

PG_DIR="/etc/postgresql"
PG_VERSION="$(ls "$PG_DIR" | sort -V | tail -n 1)"
PG_CONF_DIR="$PG_DIR/$PG_VERSION/main"
PG_CONF_FILE="$PG_CONF_DIR/postgresql.conf"
PG_HBA_FILE="$PG_CONF_DIR/pg_hba.conf"

if [ ! -d "$PG_CONF_DIR" ]; then
  echo "Nie znaleziono katalogu konfiguracji PostgreSQL: $PG_CONF_DIR"
  exit 1
fi

echo "Konfiguruję PostgreSQL w $PG_CONF_DIR..."

if grep -q "^[[:space:]]*listen_addresses[[:space:]]*=[[:space:]]*'\*'" "$PG_CONF_FILE"; then
  echo "listen_addresses już ustawione na '*'"
else
  if grep -q "^[[:space:]]*listen_addresses[[:space:]]*=" "$PG_CONF_FILE"; then
    sed -i "s/^[[:space:]]*listen_addresses[[:space:]]*=.*/listen_addresses = '*'/'" "$PG_CONF_FILE"
  else
    echo "listen_addresses = '*'" >> "$PG_CONF_FILE"
  fi
  echo "Ustawiono listen_addresses = '*'"
fi

if grep -q "^host[[:space:]]\+all[[:space:]]\+all[[:space:]]\+0\.0\.0\.0/0[[:space:]]\+md5" "$PG_HBA_FILE"; then
  echo "Reguła pg_hba.conf dla zdalnego dostępu już istnieje"
else
  echo "host all all 0.0.0.0/0 md5" >> "$PG_HBA_FILE"
  echo "Dodano regułę pg_hba.conf dla zdalnego dostępu"
fi

if grep -q "^host[[:space:]]\+all[[:space:]]\+all[[:space:]]\+127\.0\.0\.1/32[[:space:]]\+md5" "$PG_HBA_FILE"; then
  echo "Reguła pg_hba.conf dla localhost już istnieje"
else
  echo "host all all 127.0.0.1/32 md5" >> "$PG_HBA_FILE"
  echo "Dodano regułę pg_hba.conf dla localhost"
fi

echo "Restartuję PostgreSQL..."
systemctl restart postgresql

DB_NAME="autocore"
DB_USER="postgres"
DB_PASSWORD="admin"

if sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  echo "Baza danych '$DB_NAME' już istnieje"
else
  sudo -u postgres psql -c "CREATE DATABASE $DB_NAME"
  echo "Utworzono bazę danych '$DB_NAME'"
fi

sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD'"
echo "Ustawiono hasło użytkownika $DB_USER"

HOST_IP="$(hostname -I | awk '{print $1}')"
if [ -z "$HOST_IP" ]; then
  HOST_IP="127.0.0.1"
fi

CONFIG_FILE="$PROJECT_DIR/config.ini"
cat > "$CONFIG_FILE" <<EOF
[PostgreSQL]
host = $HOST_IP
port = 5432
dbname = $DB_NAME
user = $DB_USER
password = $DB_PASSWORD
EOF

echo "Utworzono plik konfiguracyjny: $CONFIG_FILE"

PHOTOS_LINK="$PROJECT_DIR/photos"
if [ -e "$PHOTOS_LINK" ] && [ ! -L "$PHOTOS_LINK" ]; then
  echo "Uwaga: $PHOTOS_LINK istnieje i nie jest symlinkiem. Nie nadpisano go."
else
  rm -f "$PHOTOS_LINK" || true
  ln -s "$SHARE_DIR" "$PHOTOS_LINK"
  echo "Utworzono symlink zdjęć: $PHOTOS_LINK -> $SHARE_DIR"
fi

echo ""
echo "Gotowe. Sprawdź połączenie" 
echo "  sudo ss -ltnp | grep 5432"
echo "  sudo -u postgres psql -h $HOST_IP -U $DB_USER -d $DB_NAME"
echo "Jeśli chcesz podłączyć inny host PostgreSQL, edytuj $CONFIG_FILE lub użyj innego hosta w kliencie."
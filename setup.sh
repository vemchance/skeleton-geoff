#!/usr/bin/env bash
# One-step setup for Geoff on a Raspberry Pi. Run from inside the geoff folder:
#   bash setup.sh
set -e
cd "$(dirname "$0")"

echo "== Checking Python =="
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9 or newer is needed. Check 'python3 --version'."; exit 1; }
python3 --version

echo "== Installing system packages (may ask for your password) =="
sudo apt-get update
sudo apt-get install -y libportaudio2 python3-venv python3-dev build-essential

echo "== Setting up Python environment (takes a few minutes) =="
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== Letting Geoff read the clicker/button =="
sudo usermod -aG input "$USER"

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "== Created config.toml (add your API keys to it) =="
fi

echo "== Audio devices =="
.venv/bin/python geoff.py --list-devices || true

echo
echo "All done. Next: add your API keys with:  nano config.toml"
echo "Reboot once before using a clicker or button:  sudo reboot"

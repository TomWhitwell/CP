#!/bin/bash
set -e  # Exit on error

echo "🛠 Updating system..."
sudo apt update && sudo apt full-upgrade -y

echo "📦 Installing dependencies..."
sudo apt install -y python3-pip python3-venv git build-essential flashrom

echo "🔌 Enabling SPI..."
sudo raspi-config nonint do_spi 0

echo "Cloning the repo..."
sudo git clone https://github.com/TomWhitwell/CP
cd CP

echo "📂 Installing systemd service..."
sudo cp computer-programmer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable computer-programmer.service
sudo systemctl start computer-programmer.service

echo "✅ Setup complete."
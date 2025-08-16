# Computer Programmer 

---

## Installation


### 1. Raspberry Pi Setup 

- Use a Raspberry Pi Zero 2 W
- Use Raspberry Pi Imager to create a SD card with the OS 
- Select Raspberry Pi OS Lite 64 Bit - you don't need the desktop 
- Make sure you [set your wifi credentials](https://www.raspberrypi.com/documentation/computers/getting-started.html#raspberry-pi-imager) in the imager. I use MTM as the hostname and prog1 (or prog2 etc) as the username. Add your local wifi settings. Enable SSH. 
- Log into the Raspberry Pi over WIFI from terminal using SSH - [full instructions](https://www.raspberrypi.com/documentation/computers/remote-access.html#ssh).  


### 1. Update and install prerequisites


curl -s https://raw.githubusercontent.com/TomWhitwell/CP/main/install.sh | bash




```bash

# Update the system and install basics 
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-pip python3-venv git build-essential flashrom
# Turn on SPI  
sudo raspi-config nonint do_spi 0
# Copy over the repository 
git clone https://github.com/TomWhitwell/CP
cd CP
sudo cp computer-programmer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable computer-programmer.service
sudo systemctl start computer-programmer.service

````

---

### 5. Auto-start on boot with systemd

Create a log directory:

```bash
mkdir -p ~/Programmer/logs
```

Create the systemd service file:

```bash
sudo nano /etc/systemd/system/flash-programmer.service
```

Paste:

```
[Unit]
Description=Computer Programmer
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/Programmer/flash-complete.py
WorkingDirectory=/home/pi/Programmer
StandardOutput=append:/home/pi/Programmer/logs/flash-programmer.log
StandardError=append:/home/pi/Programmer/logs/flash-programmer.log
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Save and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable flash-programmer.service
sudo systemctl start flash-programmer.service
```

---

### 6. Starting and stopping the service manually

Start:

```bash
sudo systemctl start flash-programmer.service
```

Stop:

```bash
sudo systemctl stop flash-programmer.service
```

View logs:

```bash
tail -f ~/Programmer/logs/flash-programmer.log
```

If debugging, stop the service before running manually:

```bash
sudo systemctl stop flash-programmer.service
sudo python3 flash-complete.py
```

---

## Usage

1. **Power on** the device.

   * LEDs will run a short animation on startup.
   * Wait \~2 seconds before pressing buttons.
2. **CHECK button (GPIO5)**:

   * Scans all slots.
   * Slot 0 is the source chip.
   * Matching chips flash slow, mismatched or error flash fast, empty slots are off.
3. **WRITE button (GPIO6)**:

   * Reads slot 0 into `card.bin` (archived with a hash filename in `CardArchive/`).
   * Writes to all matching chips, verifying after each write.
   * Sets LEDs accordingly (ON = good, fast flash = failed verify).

---

## Pinout Summary

| Function      | GPIO | Physical Pin |
| ------------- | ---- | ------------ |
| 74HC595 Clock | 2    | 3            |
| 74HC595 Latch | 3    | 5            |
| 74HC595 Data  | 4    | 7            |
| CHECK Button  | 5    | 29           |
| WRITE Button  | 6    | 31           |
| Demux A0      | 22   | 15           |
| Demux A1      | 23   | 16           |
| Demux A2      | 24   | 18           |
| Demux A3      | 25   | 22           |
| DIP1          | 16   | 36           |
| DIP2          | 19   | 35           |
| DIP3          | 20   | 38           |
| DIP4          | 21   | 40           |

---

## Troubleshooting

### Busy GPIO pins

If you see:

```
OSError: [Errno 16] Device or resource busy
```

It usually means another process is using the pins.
Possible causes:

* I²C is enabled and something is bound to GPIO2/3.
* Another process is still holding those pins.

To fully free pins GPIO2/3:

```bash
sudo nano /boot/firmware/config.txt
```

Add or uncomment:

```
dtparam=i2c_arm=off
```

Reboot.

> Disabling I²C is **not required** unless you see the busy error.

---

### Service won't start

Run:

```bash
sudo systemctl status flash-programmer.service
```

and check the last few lines of:

```bash
tail -n 50 ~/Programmer/logs/flash-programmer.log
```

---

## Notes

* `spispeed` in the script defaults to `12000` (12 MHz). You can adjust this if needed.
* Ensure all chips are oriented correctly in their slots before starting.
* Flashrom writes are destructive — double-check before writing to any chip.

## to sync with a device 

  xargs -n1 -I{} rsync -avz --exclude='.git/' --exclude='*.tmp' . prog1@MTM.local:/home/prog1/Programmer/


## Setting drive pins high for testing: 

Sets SPI CLK pin to A0 = SPI CLK - I may have accidentally changed it earlier 
```
pinctrl set 11 a0
```
Get list of all pin settings: 

```
pinctrl get 
```


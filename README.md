# Computer Programmer 

---
## Using the Computer Programmer 

### Turn on power 
- The programmer should run from any USB C adaptor (much less sensitive than the Workshop System)
- Turn on the power switch. The Power LED will come on immediately
- Some or all of the other LEDs may come on at random. Ignore them.  
Wait 20-30 seconds for the system to boot up
Watch out for a quick LED animation, then all the LEDs turn off 

### Put in the cards 
- You can set up the cards while waiting for the system to boot 
- Put the source card in the source slot (if you want to delete the cards, just put in a blank card)
- Put the cards to be written in other slots. You don't have to use all the slots. 

### Check the cards 
- Press CHECK 
- The system checks the size on the source card and turns on the LED to indicate it's OK. 
- Then it inspects each card slot and displays the outcome: 
  - Card found, correct size = SLOW BLINK 
  - Card found, faulty or incorrect size = FAST BLINK 
  - No card found = LED OFF. 
- At this point you can either: 
  - Continue and write all the suitable cards 
  - Swap out any incorrect cards and press CHECK again 

### Write the cards 
- Now press WRITE
  - The system reads the source card - you'll see its LED flashing randomly, to indicate DATA
  - If that is successful, it starts writing them one by one - a burst of DATA then the LED turns on to indicate it's finished. 
  - Any error, reading or writing gives FAST BLINK on the card LED 
- How long does it take? 
  - 15 x 2Mb cards = Approx 50 seconds 
  - 15 x 16Mb cards = Approx 10 minutes 
- All the cards are ready when all the LEDs are ON. 

### Troubleshooting 
- The pushbuttons can be a bit insensitive 

- By default the Computer Programmer works at 14MHz. 
  - The speed is set by the tiny dip switches on the PCB. 
    - 14MHZ = 0 1 1 0
  - If you have any problems, try reducing the speed
    - 8MHz = 1 1 0 0
    - 2MHz = 0 0 0 0 

- If the reader doesn't respond or the install has failed: 
  - Login with SSH 
  - `sudo systemctl status computer-programmer.service` should return the most recent logs and errors 
  - Then just ask Tom 




## Installation & Setup

### 0. Board setup 
- Don't forget to set the DIP switches 
  - 14MHZ = 0 1 1 0 (1 to 4, UP = ON = 1)

### 1. Raspberry Pi Setup 

- Use a Raspberry Pi Zero 2 W
- Use Raspberry Pi Imager to create a SD card with the OS 
- Select Raspberry Pi OS Lite 64 Bit - you don't need the desktop 
- Make sure you [set your wifi credentials](https://www.raspberrypi.com/documentation/computers/getting-started.html#raspberry-pi-imager) in the imager. I use MTM as the hostname and prog1 (or prog2 etc) as the username. Add your local wifi settings. Enable SSH. 
- It takes 3-4 minutes for the Pi to initialise for the first time. 
- Log into the Raspberry Pi over WIFI from terminal using SSH - [full instructions](https://www.raspberrypi.com/documentation/computers/remote-access.html#ssh).  


### 2. Install and start the firmware 

```
curl -s https://raw.githubusercontent.com/TomWhitwell/CP/main/install.sh | bash
```
You should see this output, with various log details and progress bars in between. It takes 5-10 minutes to complete: 

```
🛠 Updating system...
📦 Installing dependencies...
🔌 Enabling SPI...
💾 Cloning the repo...
📂 Installing systemd service...
✅ Setup complete.

```
The firmware should start once it's all finished with a quick LED animation. 
If that doesn't happen, try running it again. 

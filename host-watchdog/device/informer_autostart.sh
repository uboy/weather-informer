#!/system/bin/sh
# Informer Kiosk Autostart on Power Connection

LOG=/data/local/tmp/kiosk_autostart.log
echo "$(date): Service started" >> "$LOG"

# Wait for Android system boot to fully complete
while [ "$(getprop sys.boot_completed)" != "1" ]; do
    sleep 3
done
sleep 5

# Ensure stay on while plugged in is enabled in Android system
settings put global stay_on_while_plugged_in 7
svc power stayon true

# Initial read
ac=$(cat /sys/class/power_supply/ac/online 2>/dev/null || echo 0)
usb=$(cat /sys/class/power_supply/usb/online 2>/dev/null || echo 0)
if [ "$ac" = "1" ] || [ "$usb" = "1" ]; then
    was_plugged=1
    # If booted while already plugged in, launch kiosk
    input keyevent 224
    input keyevent 82
    am start -n uk.nktnet.webviewkiosk/.MainActivity
else
    was_plugged=0
fi

while true; do
    ac=$(cat /sys/class/power_supply/ac/online 2>/dev/null || echo 0)
    usb=$(cat /sys/class/power_supply/usb/online 2>/dev/null || echo 0)

    if [ "$ac" = "1" ] || [ "$usb" = "1" ]; then
        plugged=1
    else
        plugged=0
    fi

    # Trigger when cable is plugged in
    if [ "$plugged" = "1" ] && [ "$was_plugged" = "0" ]; then
        echo "$(date): Power connected -> launching kiosk" >> "$LOG"
        # Turn screen on
        input keyevent 224
        # Dismiss lock screen if any
        input keyevent 82
        # Launch Informer Kiosk
        am start -n uk.nktnet.webviewkiosk/.MainActivity
    fi

    was_plugged=$plugged
    sleep 2
done

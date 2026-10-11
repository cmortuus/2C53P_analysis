#!/bin/bash
# Make the Pi survive hard power-offs (it will be powered from the scope and lose power
# whenever the scope is switched off): root filesystem on an overlay (RAM), /boot/firmware
# read-only. Nothing written after enabling persists across a reboot -- including
# deployments -- so the workflow is:
#
#   sudo bash readonly.sh off && sudo reboot      # make changes (deploy, apt, config)
#   ... deploy ...
#   sudo bash readonly.sh on  && sudo reboot      # lock it again for the strapped-on build
#
# Uses raspi-config's own overlayfs implementation (initramfs + overlay root).
# Status: `sudo bash readonly.sh status`.
set -e
case "${1:-status}" in
  on)
    raspi-config nonint enable_overlayfs
    raspi-config nonint enable_bootro
    echo "overlay root + read-only boot enabled; reboot to activate"
    ;;
  off)
    raspi-config nonint disable_overlayfs
    raspi-config nonint disable_bootro
    echo "overlay root + read-only boot disabled; reboot to activate"
    ;;
  status)
    # raspi-config's get_* print 0 for "yes" and 1 for "no" (and always exit 0)
    [ "$(raspi-config nonint get_overlay_now)" = "0" ]  && echo "overlay root: ACTIVE now" || echo "overlay root: not active now"
    [ "$(raspi-config nonint get_overlay_conf)" = "0" ] && echo "overlay root: enabled for next boot" || echo "overlay root: disabled for next boot"
    [ "$(raspi-config nonint get_bootro_now)" = "0" ]   && echo "boot partition: read-only now" || echo "boot partition: read-write now"
    ;;
  *) echo "usage: $0 on|off|status"; exit 1;;
esac

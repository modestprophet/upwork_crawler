#!/usr/bin/env bash
# Entry modes for the Upwork crawler container.
#
#   cron    (default) run scheduled headless crawls via cron
#   login             start a virtual display + VNC so you can log in by hand
#   run  [args]       run a single crawl immediately (headless), passing args
#
set -euo pipefail

MODE="${1:-cron}"

start_display() {
  export DISPLAY=:99
  echo "[entrypoint] starting Xvfb + fluxbox + VNC on display ${DISPLAY}"
  Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
  sleep 1
  fluxbox >/dev/null 2>&1 &
  x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -bg -quiet
  # noVNC web client: browse to http://<host>:6080/vnc.html
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/dev/null 2>&1 &
  sleep 1
}

case "$MODE" in
  login)
    start_display
    echo "======================================================================"
    echo "  Open  http://<this-host>:6080/vnc.html  in your browser to view the"
    echo "  login window, sign in to Upwork, solve any Turnstile, then return to"
    echo "  THIS terminal and press ENTER."
    echo "======================================================================"
    exec python /app/crawler.py --login
    ;;

  run)
    shift || true
    exec python /app/crawler.py "$@"
    ;;

  cron)
    # cron does not inherit the container env; expose it to the cron job.
    printenv | grep -vE '^(no_proxy|HOSTNAME|HOME|PWD|SHLVL|_)=' > /etc/environment
    echo "[entrypoint] starting cron (scheduled headless crawls)"
    touch /var/log/cron.log
    cron
    exec tail -F /var/log/cron.log
    ;;

  *)
    exec "$@"
    ;;
esac

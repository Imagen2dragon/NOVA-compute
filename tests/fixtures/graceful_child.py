import sys
import time
import signal

def handle_break(signum, frame):
    if len(sys.argv) > 1:
        marker_path = sys.argv[1]
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write("ACKNOWLEDGED_SIGBREAK")
    sys.exit(0)

if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, handle_break)

sys.stdout.write("READY\n")
sys.stdout.flush()

while True:
    time.sleep(0.1)

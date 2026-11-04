import sys
import time
import signal

def ignore_break(signum, frame):
    pass

if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, ignore_break)

sys.stdout.write("READY\n")
sys.stdout.flush()

while True:
    time.sleep(0.1)

"""
Test fixture: Deliberately delays output and writes a trailing fatal error line
right before/as the process terminates to test EOF output draining and trailing error detection.
"""
import sys
import time

sys.stdout.write("line 1: model loader initialized\n")
sys.stdout.flush()
time.sleep(0.05)

sys.stdout.write("line 2: prompt eval time = 50.00 ms / 10 tokens\n")
sys.stdout.flush()
time.sleep(0.05)

# Trailing line delivered right at process termination
sys.stdout.write("CUDA error: out of memory in trailing buffer\n")
sys.stdout.flush()
sys.exit(0)

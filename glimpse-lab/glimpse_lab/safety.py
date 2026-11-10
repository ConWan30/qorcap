"""Sandbox guards: post-match only, disk headroom, writes confined to the lab root."""
import os, shutil

MIN_FREE_BYTES = 3 * 1024**3        # MyTurn is tight: refuse below 3 GiB free
LAB_MAX_BYTES = 1 * 1024**3         # the lab folder itself never grows past 1 GiB (crops are pruned)

class Refused(RuntimeError):
    pass

def check_disk(path, need=MIN_FREE_BYTES):
    free = shutil.disk_usage(path).free
    if free < need:
        raise Refused(f"only {free/1024**3:.1f} GiB free (< {need/1024**3:.0f} GiB); glimpse-lab will not run")
    return free

def check_not_recording(match_dir):
    if match_dir and os.path.exists(os.path.join(match_dir, ".qorcap-run", "recording.lock")):
        raise Refused("QorCap is still recording this match; glimpse-lab runs only after the match ends")

def check_write(path, lab_root):
    p, r = os.path.realpath(path), os.path.realpath(lab_root)
    if not (p == r or p.startswith(r + os.sep)):
        raise Refused(f"write outside the lab root refused: {path}")
    return p

def dir_size(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs)

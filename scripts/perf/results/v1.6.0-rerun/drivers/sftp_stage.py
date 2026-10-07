#!/usr/bin/env python3
"""Stage the s5 CSV batches onto the in-cluster SFTP server (/upload/in chroot)."""
import glob
import sys

import paramiko

HOST, PORT, USER, PW = "172.19.0.2", 30022, "perf", "perfpw"
files = sorted(glob.glob("/tmp/opencode/perf-b/batches/csv/batch_*.csv"))
assert len(files) == 10, files

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, port=PORT, username=USER, password=PW)
sftp = cli.open_sftp()
try:
    sftp.mkdir("/upload/in")
except IOError:
    pass
try:
    sftp.mkdir("/upload/out")
except IOError:
    pass
for f in files:
    sftp.put(f, f"/upload/in/{f.rsplit('/', 1)[1]}")
print("uploaded:", len(sftp.listdir("/upload/in")), "files to /upload/in")
sftp.close()
cli.close()

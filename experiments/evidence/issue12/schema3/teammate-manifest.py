import hashlib
import os
from pathlib import Path
import stat

home = Path.home()
targets = [home / name for name in ("sdn-controller", "sdn-network", "ryu-env")]
targets += sorted(home.glob("*.sh"))

def manifest(path):
    relative = str(path.relative_to(home))
    try:
        info = path.lstat()
    except FileNotFoundError:
        print("missing", relative)
        return
    meta = (info.st_mode, info.st_uid, info.st_gid)
    if stat.S_ISLNK(info.st_mode):
        print("link", meta, relative, os.readlink(path))
    elif stat.S_ISDIR(info.st_mode):
        print("dir", meta, relative)
        for child in sorted(path.iterdir()):
            manifest(child)
    elif stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        print("file", meta, info.st_mtime_ns, digest.hexdigest(), relative)
    else:
        print("other", meta, relative)

for target in targets:
    manifest(target)

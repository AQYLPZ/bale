#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuild headroom-ai 0.42.0 wheel with a slim Requires-Dist set.

Only METADATA (Requires-Dist / Version) and the derived dist-info path and
RECORD are changed; package payload bytes are copied verbatim.
"""
import argparse
import base64
import hashlib
import os
import sys
import zipfile

OLD_VERSION = "0.42.0"
NEW_VERSION = "0.42.0.post1"
TAG = "cp310-abi3-win_amd64"
OLD_DIST = "headroom_ai-%s.dist-info" % OLD_VERSION
NEW_DIST = "headroom_ai-%s.dist-info" % NEW_VERSION
OUT_NAME = "headroom_ai-%s-%s.whl" % (NEW_VERSION, TAG)

REQUIRED = [
    "httpx>=0.24.0",
    "pydantic>=2.0.0",
    "click>=8.3.3",
    "rich>=13.0.0",
    "opentelemetry-api>=1.24.0",
    "tree-sitter-language-pack>=0.10.0,<1.0",
    "tree-sitter>=0.25.2,<0.27",
]


def sha256_bytes(data):
    return hashlib.sha256(data).digest()


def record_hash(data):
    digest = base64.urlsafe_b64encode(sha256_bytes(data)).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def split_lf_keepends(data):
    parts = data.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def rewrite_metadata(data):
    lines = split_lf_keepends(data)

    version_count = sum(1 for ln in lines if ln.startswith(b"Version: "))
    if version_count != 1:
        raise SystemExit("expected exactly 1 Version line, got %d" % version_count)

    rd_indexes = [i for i, ln in enumerate(lines) if ln.startswith(b"Requires-Dist:")]
    if not rd_indexes:
        raise SystemExit("no Requires-Dist lines found")
    first, last = rd_indexes[0], rd_indexes[-1]
    if rd_indexes != list(range(first, last + 1)):
        raise SystemExit("Requires-Dist lines are not contiguous")

    for i, ln in enumerate(lines):
        if ln.startswith(b"Version: "):
            ending = b"\r\n" if ln.endswith(b"\r\n") else b"\n"
            lines[i] = b"Version: " + NEW_VERSION.encode("ascii") + ending
            break

    new_rd = [b"Requires-Dist: " + req.encode("ascii") + b"\n" for req in REQUIRED]
    lines[first:last + 1] = new_rd
    out = b"".join(lines)

    checks = [ln for ln in split_lf_keepends(out) if ln.startswith(b"Requires-Dist:")]
    if len(checks) != len(REQUIRED):
        raise SystemExit("post-rewrite Requires-Dist count mismatch")
    if any(b"extra ==" in ln for ln in checks):
        raise SystemExit("extra-qualified Requires-Dist survived rewrite")
    if ("Version: " + NEW_VERSION).encode("ascii") not in out:
        raise SystemExit("version rewrite failed")
    return out


def build_record(entries, record_name):
    lines = []
    for name, data in entries:
        if name == record_name:
            lines.append(name + ",,")
        else:
            lines.append("%s,%s,%d" % (name, record_hash(data), len(data)))
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_record(data):
    rows = {}
    for line in data.decode("utf-8").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            raise SystemExit("malformed RECORD line: %r" % line)
        rows[parts[0]] = (parts[1], parts[2])
    return rows


def verify_source(zf):
    names = zf.namelist()
    if len(names) != len(set(names)):
        raise SystemExit("duplicate entries in source wheel")
    record_name = OLD_DIST + "/RECORD"
    rows = parse_record(zf.read(record_name))
    if set(rows) != set(names):
        raise SystemExit("source RECORD does not cover exactly the archive entries")
    for name in names:
        if name == record_name:
            continue
        want_hash, want_size = rows[name]
        data = zf.read(name)
        if want_size != str(len(data)):
            raise SystemExit("source size mismatch: %s" % name)
        if want_hash != record_hash(data):
            raise SystemExit("source sha256 mismatch: %s" % name)
    meta_name = OLD_DIST + "/METADATA"
    if rows[meta_name][0] != record_hash(zf.read(meta_name)):
        raise SystemExit("source METADATA hash mismatch")
    return names


def repack(src, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, OUT_NAME)

    with zipfile.ZipFile(src) as zin:
        verify_source(zin)
        entries = []
        record_index = None
        core_before = None
        for info in zin.infolist():
            name = info.filename
            data = zin.read(name)
            if name.startswith(OLD_DIST + "/"):
                new_name = NEW_DIST + name[len(OLD_DIST):]
            elif name == OLD_DIST:
                new_name = NEW_DIST
            else:
                new_name = name
            if name == OLD_DIST + "/METADATA":
                data = rewrite_metadata(data)
            if name == OLD_DIST + "/RECORD":
                record_index = len(entries)
                entries.append([new_name, None, info])
            else:
                if name == "headroom/_core.pyd":
                    core_before = data
                entries.append([new_name, data, info])

        if record_index is None:
            raise SystemExit("RECORD entry missing")

        record_name = entries[record_index][0]
        record_data = build_record([(n, d) for n, d, _ in entries], record_name)
        entries[record_index][1] = record_data

        if len({n for n, _, _ in entries}) != len(entries):
            raise SystemExit("output entry name collision")

        tmp_path = out_path + ".tmp"
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
            for name, data, info in entries:
                zi = zipfile.ZipInfo(name, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zi.internal_attr = info.internal_attr
                zi.create_system = info.create_system
                zi.comment = info.comment
                zout.writestr(zi, data)
        os.replace(tmp_path, out_path)

    verify_output(out_path, core_before)
    return out_path


def verify_output(path, core_before):
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit("testzip failed at %s" % bad)
        names = zf.namelist()
        if any("\\" in n or n.startswith("/") or ".." in n.split("/") for n in names):
            raise SystemExit("unsafe zip member name")
        if any(n.startswith(OLD_DIST) for n in names):
            raise SystemExit("old dist-info directory still present")
        if not any(n.startswith(NEW_DIST + "/") for n in names):
            raise SystemExit("new dist-info directory missing")

        meta = zf.read(NEW_DIST + "/METADATA")
        meta_lines = split_lf_keepends(meta)
        rd = [ln for ln in meta_lines if ln.startswith(b"Requires-Dist:")]
        if len(rd) != len(REQUIRED):
            raise SystemExit("output Requires-Dist count is %d, expected %d"
                             % (len(rd), len(REQUIRED)))
        if any(b"extra ==" in ln for ln in rd):
            raise SystemExit("extra-qualified Requires-Dist in output")
        if not any(ln == ("Version: " + NEW_VERSION + "\n").encode("ascii") for ln in meta_lines):
            raise SystemExit("output Version is not " + NEW_VERSION)

        record_name = NEW_DIST + "/RECORD"
        rows = parse_record(zf.read(record_name))
        if set(rows) != set(names):
            raise SystemExit("output RECORD does not cover exactly the archive entries")
        if rows[record_name] != ("", ""):
            raise SystemExit("output RECORD self row is not empty")
        for name in names:
            if name == record_name:
                continue
            data = zf.read(name)
            if rows[name] != (record_hash(data), str(len(data))):
                raise SystemExit("output RECORD mismatch: %s" % name)

        if core_before is None:
            raise SystemExit("source _core.pyd was not seen")
        if zf.read("headroom/_core.pyd") != core_before:
            raise SystemExit("_core.pyd changed")

        wheel = zf.read(NEW_DIST + "/WHEEL").decode("utf-8")
        if "Tag: " + TAG not in wheel:
            raise SystemExit("WHEEL tag changed")

        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        print("output:", path)
        print("entries:", len(names))
        print("requires:", len(rd))
        print("size:", os.path.getsize(path))
        print("sha256:", digest)


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="upstream headroom_ai-0.42.0 wheel")
    parser.add_argument("--out-dir", default=os.path.normpath(os.path.join(here, "..", "..", "vendor", "wheels")))
    args = parser.parse_args(argv)
    repack(os.path.abspath(args.src), os.path.abspath(args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())

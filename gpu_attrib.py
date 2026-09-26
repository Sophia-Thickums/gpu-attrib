#!/usr/bin/env python3
"""
gpu-attrib — which GPU is ACTUALLY doing the work?

A measurement, not an opinion. Answers one question on a multi-GPU Linux box:
*I have N GPUs. Which one is rendering, and how do I prove it?*

WHY THIS EXISTS
Enumerating a GPU proves nothing about which one is used. Vulkan, the compositor and
most apps open every card they can see — on a two-GPU machine you will find both GPUs
in a process's file descriptor list while only one of them did any work. Reading that
fd list and concluding "it uses both" is the single most common wrong answer here.

The kernel exposes the truth per-connection, in /proc/<pid>/fdinfo/<fd>:
    drm-pdev:        the PCI address of the device this fd is bound to
    drm-engine-gfx:  nanoseconds of graphics-engine time on THAT device
drm-engine counters are cumulative since the connection was made. Sample twice, take
the delta, and the card that moved is the worker — by name, not by index.

There is also a second, quieter trap, and this tool prints it: **the adapter ORDER
reported by Vulkan is sorted by REPORTED VRAM SIZE, not by PCI address, render-node
number, or which card drives the display.** So "card 0" is not "the first card" and
is not "the card on the monitor". Indices renumber when hardware or services change,
which is how a confident report gets published over a wrong answer. This tool always
prints the device NAME beside any index.

USAGE
    gpu-attrib                     # list GPUs + the Vulkan-reported order
    gpu-attrib --watch             # sample twice (~0.5 s) and report engine deltas
    gpu-attrib --pid 1234          # attribute one process
    gpu-attrib --pid 1234 --watch  # attribute one process, with deltas
    gpu-attrib --all               # attribute every process that holds a DRM device
    gpu-attrib --json              # machine-readable output

Zero dependencies. Standard library only. Linux. Read-only: it never writes anywhere
and needs no root for its own user's processes.

Built for digital life.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

DRM_PDEV = re.compile(r"^drm-pdev:\s*(\S+)", re.M)
DRM_ENGINE = re.compile(r"^drm-engine-([a-z0-9_]+):\s*(\d+)\s*ns", re.M)
DRM_CLIENT = re.compile(r"^drm-client-id:\s*(\d+)", re.M)

ENGINE_LABEL = {
    "gfx": "graphics",
    "compute": "compute",
    "enc": "video encode",
    "dec": "video decode",
    "dma": "copy/DMA",
    "video": "video",
}


# --------------------------------------------------------------------------- GPUs

def pci_db():
    """Map PCI address -> (description, is_display). From /sys; no lspci needed."""
    out = {}
    base = "/sys/bus/pci/devices"
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for addr in entries:
        cls_path = os.path.join(base, addr, "class")
        try:
            with open(cls_path) as fh:
                cls = fh.read().strip()
        except OSError:
            continue
        # 0x030000 = VGA/display, 0x030200 = 3D controller, 0x038000 = other display
        if not cls.startswith("0x03"):
            continue
        desc = None
        # prefer the driver's own name where the kernel provides one
        for probe in ("/sys/bus/pci/devices/%s/uevent" % addr,):
            try:
                with open(probe) as fh:
                    for line in fh:
                        if line.startswith("PCI_ID="):
                            desc = line.strip().split("=", 1)[1]
            except OSError:
                pass
        vendor = device = None
        try:
            with open(os.path.join(base, addr, "vendor")) as fh:
                vendor = fh.read().strip()
            with open(os.path.join(base, addr, "device")) as fh:
                device = fh.read().strip()
        except OSError:
            pass
        driver = None
        drv_link = os.path.join(base, addr, "driver")
        if os.path.islink(drv_link):
            driver = os.path.basename(os.readlink(drv_link))
        # VRAM as the kernel sees it (bytes, where the driver exports it)
        vram = None
        for cand in ("mem_info_vram_total",):
            p = os.path.join(base, addr, cand)
            try:
                with open(p) as fh:
                    vram = int(fh.read().strip())
            except (OSError, ValueError):
                pass
        out[addr.lower()] = {
            "addr": addr.lower(),
            "vendor": vendor,
            "device": device,
            "pci_id": desc or "",
            "driver": driver,
            "vram_bytes": vram,
            "is_display_class": cls.startswith("0x0300"),
        }
    return out


def render_nodes():
    """Map render node -> PCI address, by resolving the device symlink."""
    out = {}
    d = "/sys/class/drm"
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        if not n.startswith("renderD"):
            continue
        link = os.path.join(d, n, "device")
        try:
            real = os.path.realpath(link)
        except OSError:
            continue
        out[n] = os.path.basename(real).lower()
    return out


def vulkan_order():
    """The order Vulkan reports adapters — the sort-by-VRAM trap lives here.

    Uses vulkaninfo if present (it is the authority). Degrades silently when absent:
    a missing instrument is reported as missing, never guessed at.
    """
    import shutil
    import subprocess
    exe = shutil.which("vulkaninfo")
    if not exe:
        return None, "vulkaninfo not installed (install vulkan-tools for this section)"
    try:
        p = subprocess.run([exe, "--summary"], capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "vulkaninfo failed: %s" % e
    names = re.findall(r"deviceName\s*=\s*(.+)", p.stdout)
    return [n.strip() for n in names], None


def human(n):
    if n is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# ---------------------------------------------------------------------- attribution

def read_fdinfo(pid):
    """Return list of {fd, target, pdev, client, engines{name:ns}} for one process."""
    res = []
    fd_dir = "/proc/%d/fd" % pid
    info_dir = "/proc/%d/fdinfo" % pid
    try:
        fds = os.listdir(info_dir)
    except OSError:
        return res
    for fd in fds:
        info_path = os.path.join(info_dir, fd)
        try:
            with open(info_path) as fh:
                txt = fh.read()
        except OSError:
            continue
        m = DRM_PDEV.search(txt)
        if not m:
            continue
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            target = "?"
        engines = {}
        for ename, nsec in DRM_ENGINE.findall(txt):
            # the kernel can emit several lines per engine class; keep the largest
            engines[ename] = max(engines.get(ename, 0), int(nsec))
        cm = DRM_CLIENT.search(txt)
        res.append({
            "fd": fd,
            "target": target,
            "pdev": m.group(1).lower(),
            "client": int(cm.group(1)) if cm else None,
            "engines": engines,
        })
    return res


def processes_holding_drm():
    """PIDs with at least one fdinfo carrying drm-pdev."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if read_fdinfo(pid):
            out.append(pid)
    return sorted(out)


def comm(pid):
    try:
        with open("/proc/%d/comm" % pid) as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def attribute(pid):
    """Sample a process twice and report engine-time deltas per PCI device."""
    first = read_fdinfo(pid)
    if not first:
        return None
    time.sleep(0.5)
    second = read_fdinfo(pid)

    by_pdev = defaultdict(lambda: {"engines": defaultdict(int), "fds": 0, "clients": set()})
    # presence: every device the process has OPENED (the enumeration red herring)
    opened = defaultdict(int)
    for rec in second:
        opened[rec["pdev"]] += 1

    # deltas: keyed by (pdev, engine)
    snap1 = defaultdict(int)
    for rec in first:
        for e, ns in rec["engines"].items():
            k = (rec["pdev"], e)
            snap1[k] = max(snap1[k], ns)
    snap2 = defaultdict(int)
    for rec in second:
        for e, ns in rec["engines"].items():
            k = (rec["pdev"], e)
            snap2[k] = max(snap2[k], ns)

    for (pdev, eng), ns2 in snap2.items():
        delta = ns2 - snap1.get((pdev, eng), 0)
        if delta > 0:
            by_pdev[pdev]["engines"][eng] += delta
    for rec in second:
        if rec["client"] is not None:
            by_pdev[rec["pdev"]]["clients"].add(rec["client"])

    workers = {p: d for p, d in by_pdev.items() if d["engines"]}
    return {
        "pid": pid,
        "comm": comm(pid),
        "opened_devices": dict(opened),
        "total_fds": len(second),
        "workers": {
            p: {
                "engines": {e: ns for e, ns in d["engines"].items()},
                "clients": sorted(d["clients"]),
            }
            for p, d in workers.items()
        },
    }


# --------------------------------------------------------------------------- render

def print_gpus(gpus, nodes, order, order_err, as_json):
    if as_json:
        return
    print("=" * 72)
    print("GPUS PRESENT (from /sys — this is hardware, not an API)")
    print("=" * 72)
    if not gpus:
        print("  no display-class PCI devices found")
    for addr, g in sorted(gpus.items()):
        rn = [n for n, a in nodes.items() if a == addr]
        vram = human(g["vram_bytes"]) if g["vram_bytes"] else "not exported by driver"
        print("  %-14s %s" % (addr, g["pci_id"] or "(unknown)"))
        print("  %-14s driver=%s  vram=%s  render=%s" %
              ("", g["driver"] or "?", vram, ",".join(rn) or "—"))

    print()
    print("=" * 72)
    print("THE ORDER VULKAN REPORTS ADAPTERS  ← indices are NOT identities")
    print("=" * 72)
    if order_err:
        print("  %s" % order_err)
    else:
        for i, name in enumerate(order):
            print("  adapter[%d]  %s" % (i, name))
        print()
        print("  ★ This list is sorted by REPORTED VRAM SIZE, not by PCI address, not by")
        print("    render-node number, and not by which card drives your display. \"Card 0\"")
        print("    is therefore not \"the first card\" and not \"the card on the monitor\".")
        print("    Always match on the NAME above before acting on an index.")


def print_attr(rows, gpus, as_json):
    if as_json:
        return
    print()
    print("=" * 72)
    print("WHO ACTUALLY RENDERED  (engine-time delta over ~0.5 s)")
    print("=" * 72)
    if not rows:
        print("  no processes with DRM connections were visible to this user")
        return
    for r in rows:
        print("  pid %-7d %s" % (r["pid"], r["comm"]))
        print("      opened %d device(s) / %d DRM fd(s)   ← enumeration only, proves nothing"
              % (len(r["opened_devices"]), r["total_fds"]))
        if not r["workers"]:
            print("      engine time in this window: NONE on any device")
            print("      → it holds GPU handles but did no work while sampled. It is idle.")
        else:
            for pdev, d in r["workers"].items():
                g = gpus.get(pdev, {})
                label = g.get("pci_id") or pdev
                eng = ", ".join("%s %s" % (ENGINE_LABEL.get(e, e), _ms(ns))
                                for e, ns in sorted(d["engines"].items()))
                print("      ★ WORKER: %s  (%s)   %s" % (label, pdev, eng))
            idle = set(r["opened_devices"]) - set(r["workers"])
            if idle:
                print("      idle despite being open: %s" % ", ".join(sorted(idle)))
        print()


def _ms(ns):
    if ns >= 1e9:
        return "%.2f s" % (ns / 1e9)
    if ns >= 1e6:
        return "%.2f ms" % (ns / 1e6)
    if ns >= 1e3:
        return "%.1f µs" % (ns / 1e3)
    return "%d ns" % ns


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Which GPU is actually doing the work? Measured, not guessed.")
    ap.add_argument("--pid", type=int, action="append", default=None,
                    help="attribute this process (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="attribute every process holding a DRM device")
    ap.add_argument("--watch", action="store_true",
                    help="sample twice and report engine-time deltas")
    ap.add_argument("--gpus-only", action="store_true", help="skip attribution")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not sys.platform.startswith("linux"):
        print("gpu-attrib: Linux only (it reads /proc and /sys).", file=sys.stderr)
        return 2

    gpus = pci_db()
    nodes = render_nodes()
    order, order_err = vulkan_order()

    rows = []
    want_attr = (args.pid or args.all) and not args.gpus_only
    if args.all:
        pids = processes_holding_drm()
    elif args.pid:
        pids = list(args.pid)
    else:
        pids = []
    for pid in pids:
        r = attribute(pid) if args.watch else _snapshot(pid)
        if r:
            rows.append(r)

    if args.json:
        print(json.dumps({
            "gpus": gpus,
            "render_nodes": nodes,
            "vulkan_order": order,
            "vulkan_order_error": order_err,
            "attribution": rows,
            "samples": 2 if args.watch else 1,
        }, indent=2))
        return 0

    print_gpus(gpus, nodes, order, order_err, args.json)
    if want_attr:
        print_attr(rows, gpus, args.json)
        if not args.watch:
            print("  (single snapshot — cumulative counters only. Use --watch for deltas.)")
    elif not args.gpus_only:
        print()
        print("  Add --watch --all to see which process is actually rendering right now.")

    # A useful exit code: 0 always for a successful measurement — this is a probe,
    # not a gate. Callers that want a verdict read the JSON.
    return 0


def _snapshot(pid):
    """One-pass view without deltas (cumulative counters only)."""
    recs = read_fdinfo(pid)
    if not recs:
        return None
    opened = defaultdict(int)
    workers = {}
    for rec in recs:
        opened[rec["pdev"]] += 1
        if rec["engines"]:
            w = workers.setdefault(rec["pdev"], {"engines": defaultdict(int), "clients": set()})
            for e, ns in rec["engines"].items():
                w["engines"][e] = max(w["engines"][e], ns)
            if rec["client"] is not None:
                w["clients"].add(rec["client"])
    return {
        "pid": pid,
        "comm": comm(pid),
        "opened_devices": dict(opened),
        "total_fds": len(recs),
        "workers": {p: {"engines": dict(d["engines"]), "clients": sorted(d["clients"])}
                    for p, d in workers.items()},
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

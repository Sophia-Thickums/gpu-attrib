# gpu-attrib

**Which GPU is actually doing the work?** One question, answered with a number — on any
multi-GPU Linux box, with zero dependencies.

```
$ gpu-attrib --watch --all
  pid 33419   vkcube
      opened 3 device(s) / 4 DRM fd(s)   ← enumeration only, proves nothing
      ★ WORKER: 1002:7550  (0000:03:00.0)   graphics 3.64 ms
      idle despite being open: 0000:0f:00.0, 0000:10:00.0
```

That output is the whole point. The process had **all three** GPUs open. Exactly one did
any work, and the tool names it — by PCI address, with the engine time it actually spent.

---

## The two mistakes this tool exists to stop

### 1. "It has both GPUs open, so it uses both"

It doesn't. Vulkan, the compositor, and most applications open **every** device they can
see. On a two-GPU machine you will find both cards in a process's file descriptor list
while only one of them ever renders a pixel. Reading that list and concluding "it uses
both" is the most common wrong answer in this area, and it is the reason bug reports like
*"my second card is always at 0% but the game is slow"* go nowhere for weeks.

**A file descriptor is a handle. It is not evidence of work.**

### 2. "Card 0 is the first card"

Adapter indices are **not** identities. They renumber when hardware, drivers, or services
change, and the number you read in one tool does not reliably match the number another tool
uses. Worse: **`vulkaninfo` reports adapters in an order that has nothing to do with PCI
address, render-node number, or which card drives your display.** The order is a driver
implementation detail.

Indices are for *indexing*. When you act, act on the **name** or the **PCI address**.

---

## How it measures

The kernel exposes the truth per DRM connection, in `/proc/<pid>/fdinfo/<fd>`:

| field | meaning |
|---|---|
| `drm-pdev` | the PCI address this fd is bound to |
| `drm-engine-gfx` | nanoseconds of graphics-engine time **on that device** |
| `drm-client-id` | the kernel's id for this connection |

The counters are cumulative for the life of the connection. **Sample twice, subtract, and
the card whose counter moved is the worker.** That's it — no driver support required, no
root needed for your own processes, and it works identically on AMD, Intel and NVIDIA
because it reads the kernel, not a vendor API.

The tool prints the *opened* device set and the *worked* device set **separately and
deliberately**, because the gap between them is the entire lesson.

---

## Usage

```bash
gpu-attrib                      # list GPUs + the adapter order the driver reports
gpu-attrib --watch --all        # sample every process twice (~0.5 s) and show deltas
gpu-attrib --pid 1234 --watch   # attribute one process
gpu-attrib --json               # machine-readable
```

Python 3.8+, standard library only, Linux only (it reads `/proc` and `/sys`). Read-only —
it never writes anywhere and changes nothing.

`--json` emits `gpus`, `render_nodes`, `vulkan_order`, and per-process `attribution`
with raw nanosecond deltas, so you can gate on it in a script instead of parsing prose.

---

## A worked example, including a result that contradicted what we believed

This tool was written to test a claim we had recorded ourselves: *"on this box the driver
orders adapters by reported VRAM size, so the card with more VRAM becomes adapter 0 and a
default-picking application renders on it."*

Measured on this machine, 2026-09-26, Mesa 26.2.3 / RADV, kernel 7.2.7:

| GPU | PCI address | VRAM (from `/sys`) | adapter index | engine time under a default-picking app |
|---|---|---|---|---|
| RX 9070 XT | `0000:03:00.0` | 16,304 MiB | **0** | **3.64 ms — the worker** |
| Vega Frontier Edition | `0000:0f:00.0` | 16,368 MiB | 1 | none |
| Ryzen iGPU | `0000:10:00.0` | 512 MiB | 2 | none |

**The card with MORE VRAM was listed SECOND, and a default-picking Vulkan app rendered on
the card with LESS.** The recorded claim did not reproduce.

What that means, stated carefully: the claim is **not** confirmed on this configuration, and
the honest thing is to mark it as unconfirmed rather than to quietly delete it or to
confidently invert it. Two things were deliberately **not** tested here and must not be
assumed either way — the `vkd3d-proton`/D3D12 path, and the compositor's own selection
(which is a different mechanism entirely: it can be steered by kernel cmdline and by
`KWIN_DRM_DEVICES`, and is not governed by Vulkan enumeration at all).

**This is the tool's actual value:** a belief that had been sitting in our notes for a week
took ten seconds to test, and it was wrong. A number you can run beats a note you trust.

---

## Related

- **`chiasm`** — cross-GPU `dma_buf` handoff, with the benchmark that beat its own thesis.
- **`cf-probe`** — can these cards be made to cooperate at the presentation layer at all?
  One enumeration call, yes/no, ~2 seconds.

Built for digital life.

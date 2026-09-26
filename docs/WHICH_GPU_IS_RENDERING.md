# Which GPU is actually rendering?

*Two ways to know, and one belief of mine that didn't survive the test.*

On a multi-GPU Linux box, the question "which card is doing the work?" gets answered wrongly
almost every time — and both wrong answers feel like evidence. This is the method that
answers it with a number, plus a measurement that overturned something I had written down
as settled.

---

## Wrong answer #1: "it has both GPUs open, so it uses both"

Open any GPU-using process on a two-card machine and look at its file descriptors. You will
find *both* cards. Here is a real example — a trivial Vulkan demo on a box with a discrete
card, a compute card, and an integrated GPU:

```
fd 5:  drm-pdev: 0000:10:00.0        # integrated
fd 6:  drm-pdev: 0000:0f:00.0        # second discrete card
fd 7:  drm-pdev: 0000:03:00.0        # primary discrete card
fd 8:  drm-pdev: 0000:03:00.0        #   ...and only THIS one has engine time
       drm-engine-gfx: 42866351 ns
```

All three devices are open. Exactly one has engine time. **A file descriptor is a handle,
not evidence of work** — drivers and the Vulkan loader enumerate and open every device they
can see, whether or not a single pixel is rendered on it. This is why bug reports like *"my
second card sits at 0% but the game is slow"* stall for weeks: the reporter has already
"confirmed" both cards are in use.

## Wrong answer #2: "card 0 is the first card"

Adapter indices are not identities. They reorder when hardware, drivers or services change,
and the number one tool prints does not reliably match another's. Worse, `vulkaninfo`'s
adapter order has nothing to do with PCI address, render-node number, or which card drives
your display — it is a driver implementation detail, not a physical fact.

Act on the **name** or the **PCI address**. Never on an index.

---

## The method

The kernel keeps the truth per DRM connection in `/proc/<pid>/fdinfo/<fd>`:

| field | meaning |
|---|---|
| `drm-pdev` | the PCI address this fd is bound to |
| `drm-engine-gfx` | nanoseconds of graphics-engine time **on that device** |
| `drm-client-id` | the kernel's id for the connection |

Counters are cumulative for the life of the connection, so **sample twice and subtract**.
The card whose counter moved is the worker. No vendor API, no driver support, no root needed
for your own processes — it reads the kernel, so it works the same on AMD, Intel and NVIDIA.

I packaged this as a zero-dependency tool, [`gpu-attrib`](https://github.com/Sophia-Thickums/gpu-attrib):

```
$ gpu-attrib --watch --all

  pid 36417   vkcube
      opened 3 device(s) / 4 DRM fd(s)   ← enumeration only, proves nothing
      ★ WORKER: 1002:7550  (0000:03:00.0)   graphics 3.71 ms
      idle despite being open: 0000:0f:00.0, 0000:10:00.0
```

It prints the *opened* set and the *worked* set separately and deliberately, because the gap
between them is the entire lesson.

---

## The part worth reading: a belief that didn't reproduce

I had recorded — after real debugging, and written down as a rule — that this driver orders
adapters **by reported VRAM size**: that a card reporting 16,368 MiB would take adapter 0
ahead of one reporting 16,304 MiB, so any application taking "the first device" would render
on the older, slower card, and that this explained a class of slowness.

It is a *plausible* mechanism, it fit an observed symptom, and it had lasted a week in my
notes. Then I measured it properly. **Mesa 26.2.3 / RADV, kernel 7.2.7, three AMD devices:**

| GPU | PCI address | VRAM (`/sys`) | adapter index | engine time, default pick |
|---|---|---|---|---|
| RX 9070 XT | `0000:03:00.0` | 16,304 MiB | **0** | **3.71 ms — the worker** |
| Vega Frontier Edition | `0000:0f:00.0` | **16,368 MiB** | 1 | none |
| Ryzen iGPU | `0000:10:00.0` | 512 MiB | 2 | none |

The card with **more** VRAM was listed **second**. And a default-picking application with a
clean environment chose the card with **less**.

**Corroborated by a second, independent instrument**, because one measurement can be an
artifact: the demo prints its own selection,
`Selected GPU 0: AMD Radeon RX 9070 XT (RADV GFX1201)`. Enumeration order and default pick
agree with each other, and both contradict the recorded rule.

### What that finding is, and is not

- The rule is **not confirmed** on this configuration. That is all it means so far.
- **Untested, and not to be assumed either way:** the `vkd3d-proton`/D3D12 path, and the
  compositor's own device selection — the latter is a *different mechanism* (steerable by
  kernel cmdline and `KWIN_DRM_DEVICES`), not governed by Vulkan enumeration at all.
- **What survives unchanged:** pinning by name is still the right habit. If you want a
  specific card, set `MESA_VK_DEVICE_SELECT=<vendor:device>` — and note the trailing `!`
  makes it *exclusive*, exposing only that device. `DRI_PRIME` will not do this for Vulkan
  or D3D12; nor will `VK_LOADER_DEVICE_SELECT` for `vkd3d-proton`, which enumerates Vulkan
  itself and ignores the loader's picker.

A rule that fits a symptom is a hypothesis. It took ten seconds to test one that had been
sitting in my notes for a week, and it was wrong. That is the argument for shipping
instruments instead of notes — and the honest reason this tool exists.

---

## Related measurements

- **[cf-probe](https://github.com/Sophia-Thickums/cf-probe)** — can two cards be made to
  cooperate at the presentation layer *at all*? One Vulkan call, yes/no, ~2 seconds. On RADV
  across three GPU generations: every physical-device group reports `deviceCount = 1`, so
  alternate-frame multi-GPU is dead on arrival — decided in one enumeration, before any
  question of performance.
- **[chiasm](https://github.com/Sophia-Thickums/chiasm)** — cross-GPU `dma_buf` handoff
  without a host copy, **with the benchmark that beat its own thesis** published in the
  README.

Measurements on CachyOS, kernel `7.2.7-1-cachyos`, Mesa `26.2.3-1`, RADV.

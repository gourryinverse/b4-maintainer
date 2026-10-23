# vng: NUMA topologies, fake memory devices, and qemu directly

For mm and memory-tiering work. The session rules in [../SKILL.md](../SKILL.md) apply:
commands through the sandbox wrapper, outputs under `out/vng/`.

## NUMA topology and fake memory devices

For mm work you usually want more nodes than the host has:

```sh
vng -r bzImage --cpus 4 --memory 8G \
    --numa 4G,cpus=0-3 --numa 4G \
    --append 'memmap=4G!0x140000000'
```

- Each `--numa <size>[,cpus=...]` adds a node; node physical ranges are the cumulative
  sizes.
- `memmap=SIZE!OFFSET` carves a range out as **persistent memory** (e820 type 12).
  Point it at a node's range and that node becomes a pmem/dax node: the usual way to
  fabricate a memoryless device node for tiering work.

> **Do not compute the offset from the `--numa` sizes.** The PCI hole shifts node
> bases. In the example above node1 begins at **0x140000000 (5G)**, not 4G. Verify
> against `start_pfn` in `/proc/zoneinfo`, or `/proc/iomem`, and have the guest runner
> print both: a wrong offset carves the wrong node, which looks like a test bug for a
> long time.

Attaching disks (for swap or a filesystem under test) needs virtio; the plain `--disk`
option gives "block 0 write restricted" and `mkfs` stalls:

```sh
--qemu-opts='-drive file=/path/swap.img,format=raw,if=virtio'
```

## Worked example: fabricating a dax/kmem NUMA node

Turns a carved pmem range into a hotpluggable NUMA node.

```sh
modprobe -q nd_e820; modprobe -q dax_pmem; modprobe -q device_dax
modprobe -q nd_pmem;  modprobe -q kmem

for r in $(ndctl list -R | grep -oE 'region[0-9]+'); do
        ndctl create-namespace -m devdax -e "${r/region/namespace}.0" -f
done

D=/sys/bus/dax/devices/dax1.0        # pick by: cat $D/target_node
echo dax1.0 > /sys/bus/dax/drivers/device_dax/unbind
echo dax1.0 > /sys/bus/dax/drivers/kmem/new_id   # new_id, NOT bind: auto-binds
```

Then online the resulting memory blocks `online_movable`. **Never `online_kernel` a
node you intend to offline later**: kernel-zoned memory collects unmovable
allocations, offline returns `-EBUSY` forever, and the device wedges.

- `modprobe -q A B C` treats **B and C as module parameters**, not further modules.
  Only A is loaded; this is invisible until one of them is built as a module rather
  than built in. One `modprobe` per module.
- `ndctl` must exist in the guest. Without it `ndctl list -R` is empty, no dax device
  appears, and every test SKIPs with no clue.

## What vng cannot do

vng 1.41 cannot express hugetlb-backed memory, `memory-backend-file`, `prealloc`, or
`host-nodes` binding. If guest memory must *actually* be backed by a specific host NUMA
node (the only way to measure real cross-node or CXL latency), drive qemu directly.
This needs host hugepages and may not be possible inside the sandbox; if not, say so
and propose the command for the maintainer instead.

```sh
qemu-system-x86_64 -machine q35 -m 32G \
  -object memory-backend-file,mem-path=/dev/hugepages1G,size=24G,prealloc=on,\
host-nodes=0,policy=bind,id=ram0 \
  -object memory-backend-file,mem-path=/dev/hugepages1G,size=8G,prealloc=on,\
host-nodes=1,policy=bind,id=ram1 \
  -numa node,memdev=ram0 -numa node,memdev=ram1 \
  -kernel bzImage -initrd <initramfs> \
  -append "earlycon=uart8250,io,0x3f8 console=ttyS0,115200 panic=1 rdinit=/init" \
  -display none -serial stdio
```

Use `-display none -serial stdio`, not a bare `-nographic`: the latter can give zero
output. qemu's automatic NUMA balancing is off at boot; add `numa_balancing=` to
`-append` if you need promotion.

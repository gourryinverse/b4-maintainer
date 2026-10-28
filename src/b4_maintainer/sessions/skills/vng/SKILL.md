---
name: vng
description: Build the session's kernel tree and run selftests or reproducers in a virtme-ng (vng) VM. Use when a task needs the series running, not just read; covers the harness, capture, build rules, and the gotchas that make runs silently test nothing.
---

# Testing kernel changes under virtme-ng (vng)

A field guide for driving kernel selftests in a VM. Written against **virtme-ng
1.41** on x86_64; check `vng --version` and `vng --help` before relying on an option.
Most of these failures are *silent*: a green run that tested nothing, or an empty log
with no error. The gotchas and the checklist are the parts that matter.

## In this session

- Shell commands already run in the session's sandbox: the worktree is writable,
  `/dev/kvm` is there when the host has it, and the system's `vng`, compilers, and QEMU
  are on `PATH`. There is no network: work with what is in the tree and on the host.
- Work from the top of the session worktree; write launchers, guest runners, and
  captured logs under the session's `out/vng/`, not `/tmp`.
- Stop only the VMs you started, by PID. Never `pkill` qemu by name: other VMs on the
  machine are not yours.
- If the VM cannot boot (no `/dev/kvm`, no QEMU, no vng), say so and stop rather than
  substituting another method.

## 1. What vng is good for

`vng` boots the kernel you just built using the **host's root filesystem over 9p**:
no image to build, no rootfs to maintain, boot to shell in a few seconds. Use it for:

- running kernel selftests against a freshly built tree;
- reproducing an mm/ or driver bug with an exotic NUMA or memory topology;
- bisecting behaviour across commits (boot cost is low enough to loop).

It is the **wrong** tool for performance numbers: guest memory is ordinary host DRAM
whatever you tell it, so any "tier" or "device" built with `memmap=` has DRAM latency.
Measure performance on real hardware, or with qemu directly (see
[reference/numa-and-qemu.md](reference/numa-and-qemu.md)).

## 2. Minimum viable invocation

```sh
vng -r ./arch/x86/boot/bzImage --cpus 4 --memory 8G --exec 'bash /path/to/test.sh'
```

**`-r <bzImage>` is not optional.** A bare `vng` (or `vng -r` with no argument in some
shells) boots the *host's running kernel*: your code is absent, the sysfs attributes
your test looks for don't exist, and every test SKIPs. A suite reporting "all skipped"
is this nine times out of ten. Assert it in the guest:

```sh
echo "# kernel: $(uname -r)"     # must match your build, not the host's
```

## 3. The harness pattern

Split it in two: a **host launcher** that owns the topology and the capture, and a
**guest runner** that owns the test list and the verdicts. The guest's `/tmp` is tmpfs
and dies with the VM, so the guest prints everything it wants kept to stdout.

### Host launcher

```sh
#!/bin/bash
set -u
TREE=$(pwd)                     # run from the top of the session worktree
OUT=/path/to/session/out/vng    # the session's out/vng (absolute)
KERNEL=$TREE/arch/x86/boot/bzImage
LOG=${1:-$OUT/run.log}

[ -f "$KERNEL" ] || { echo "no bzImage at $KERNEL"; exit 2; }

rm -f "$LOG"
script -q -c "vng -r '$KERNEL' --cpus 4 --memory 8G \
    --append 'panic=0' \
    --exec 'bash $OUT/guest_run.sh $TREE'" "$LOG"

echo "=== captured to $LOG ==="
```

`script -q -c` is there because vng buffers heavily; without a pty you may see nothing
until exit. If `script` is missing (Fedora: util-linux-script), run vng directly with
`> "$LOG" 2>&1`: the guest falls back to the serial console and the output still arrives. The `--exec` argument is single-quoted *inside* the double-quoted
`script -c` string; getting this wrong gives a VM that boots and exits with no output.
After editing that line, `bash -n` the launcher: a mismatched quote is easy and silent.

### Guest runner

```sh
#!/bin/bash
TREE=$1; shift                      # the worktree, from the launcher
cd "$TREE/tools/testing/selftests/<subsys>" || exit 1
PROGS=$(sed -n 's/^TEST_PROGS *[:+]\?= *//p' Makefile)
[ $# -gt 0 ] && PROGS="$*"          # allow running a subset

echo "===RUN-BEGIN==="
echo "# kernel: $(uname -r)"
pass=0 skip=0 fail=0 faillist=""
for t in $PROGS; do
        [ -f "$t" ] || continue
        echo "#### RUN $t ####"
        echo "### RUN $t" > /dev/kmsg 2>/dev/null   # attribute later splats
        timeout "${TEST_TIMEOUT:-220}" bash "./$t"
        rc=$?
        echo "## $t rc=$rc ##"
        case $rc in
        0) pass=$((pass+1)) ;;
        4) skip=$((skip+1)) ;;                      # KSFT_SKIP
        *) fail=$((fail+1)); faillist="$faillist $t($rc)" ;;
        esac
done
echo "===SUMMARY=== pass=$pass skip=$skip fail=$fail fails=[$faillist ]"
echo "===DMESG-BEGIN==="
dmesg 2>/dev/null                                   # required: see section 4
echo "===DMESG-END==="
echo "===RUN-END==="
```

Worth copying:

- **Parse `TEST_PROGS` with `sed`; don't `include Makefile`.** Including it drags in
  `lib.mk`'s default goal, so you get build output instead of a test list.
- **`echo "### RUN $t" > /dev/kmsg`** interleaves the test name into the kernel log,
  so a splat or OOM kill in `dmesg` names the test that caused it.
- **Make the timeout an environment variable.** Under a debug config the suite runs
  5-10x slower, and a hardcoded timeout turns that into a mysterious `rc=124`.

Machine-readable markers (`## <test> rc=N ##`, sentinels around blocks) let you grade
a run with `grep -c` instead of reading it. `rc=124` is `timeout`, not a test failure:
check that before investigating.

## 4. Capture: the trap that invalidates results

**vng routes the kernel console to `/dev/null`.** Only the `--exec` process's stdout is
captured, so grepping the captured log for `BUG:`/`WARNING:`/`KASAN` proves
**nothing**: those go to the kernel log buffer, which was never captured. The guest
runner must dump `dmesg` itself. To attribute splats as they happen, also stream them
into a shared directory (survives a wedge, unlike a final dump):

```sh
dmesg -w > "$RWDIR/dmesg.log" &
```

- **`panic=-1`.** vng sets it, so an oops *reboots the VM away before the message is
  printed*: a bare non-zero exit, an empty log, nothing to diagnose. Pass
  `--append 'panic=0'` so it hangs with the message on screen.
- **False-positive splat greps.** A case-insensitive `kasan` matches a worktree path
  containing "kasan" in the boot cmdline. Grep case-sensitively for `KASAN:` / `BUG:` /
  `WARNING:`, and confirm the sanitiser ran:
  `dmesg | grep 'KernelAddressSanitizer initialized'`.

Sharing a writable directory with the host: only the plain form works.

```sh
--rwdir=$OUT/share            # works
--rwdir=guest=host            # documented, but vng 1.41 exits 1 with no output
```

## 5. Build rules

**Build both the kernel and modules**, always:

```sh
make -j"$(nproc)" bzImage modules
```

vng uses the in-tree modules; there is no `make modules_install` step. Skipping
`modules` fails nastily: with `CONFIG_MODVERSIONS`, editing a *built-in* file changes
symbol CRCs, the stale `.ko` files fail to load with
`disagrees about version of symbol (err -22)`, and their subsystem silently isn't
there. Every test SKIPs and nothing says why. Check `dmesg | grep 'Unknown symbol'` when
a subsystem vanishes.

**Selftest binaries are not built by the runner.** The guest runs whatever pre-built
binaries sit in the selftests directory. After editing a `*_tool.c`, run
`make -C tools/testing/selftests/<subsys>` on the host, or the guest silently runs the
stale one. When a change "has no effect", verify:

```sh
strings tools/testing/selftests/<subsys>/<tool> | grep '<a string you just added>'
```

`CONFIG_BOOT_CONFIG_EMBED=y` breaks builds in a fresh worktree; turn it off.

## 6. Gotchas that cost real time

- **Stuck VMs.** vng's process comm is truncated to 15 characters
  (`qemu-system-x86`), so `pkill -x qemu-system-x86_64` never matches, and stuck VMs
  accumulate until new runs stall at zero bytes with no error. Record the PID of each
  VM you start and kill that PID; do not kill by name (see "In this session").
- **Guest `/` is read-only 9p; guest `/tmp` is tmpfs.** Anything written outside an
  `--rwdir` is gone when the VM exits. Print it, or share a directory.
- **`git checkout --force` leaves untracked build artifacts.** Looping over commits, a
  stale selftest binary from a *later* commit satisfies the current commit's `make` and
  hides a real breakage. `git clean -qxfd <selftest dir>` between iterations.
- **Debug configs change outcomes, not just speed.** Under `CONFIG_KASAN`, freed
  objects sit in quarantine and pin pages, so memory-offline and hot-unplug tests
  *false-fail*; validate unplug paths on a non-KASAN kernel. `CONFIG_PAGE_TABLE_CHECK`
  adds per-PTE atomics: write-heavy tests that pass in 200s may need 900s.
- **A suite of only SKIPs is a setup failure, not a clean run.** Usual causes, in
  order: booted the host kernel (section 2), stale modules (section 5), or the device
  or topology the tests need was never provisioned.

NUMA topologies, fake memory devices, dax/kmem nodes, and driving qemu directly are in
[reference/numa-and-qemu.md](reference/numa-and-qemu.md); read it only for mm or
memory-tiering work.

## 7. Checklist before believing a green run

1. Did the guest print *your* `uname -r`?
2. Did the guest dump `dmesg`, and did you grep it case-sensitively for `BUG:` /
   `WARNING:` / `KASAN:`?
3. Is the pass count what you expected, or is it mostly SKIPs?
4. Any `rc=124`? That's a timeout, not a result.
5. If you changed a selftest tool, did you rebuild it on the host?
6. If the topology matters, did the guest print `/proc/iomem` and per-node `meminfo`
   so you can confirm the carve landed where you think?

## Reporting

- Keep the launcher, guest runner, and full captured log in `out/vng/`, and report the
  exact commands, the guest's `uname -r`, and the summary line.
- To show that a series fixes a problem, run the reproducer on the base and on the
  series, and report both.

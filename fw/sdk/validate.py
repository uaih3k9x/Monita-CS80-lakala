#!/usr/bin/env python3
"""Static check of a packed 2PPA app against the R05 loader rules. Does NOT touch hardware.

Rules below are read from the R05 AP loader (see docs/r05-notes.md §3):
  magic 2PPA; 0x80 < total_len; base+total_len <= 0x60370000
  version: hi16 == kernel hi16 (原厂 app = 0x0001), lo16 <= kernel lo16 (原厂 = 0)
  enter != 0; enter/exit/@0x18/@0x1c (if !=0) in [base, base+total) or [0x80fe0000, 0x81000000)
  crc32@8 = zlib.crc32(whole file with @8 = 0)
  6 descriptor slots @0x20..0x80, kind 0 or size 0 = skipped; kind 1..4 only
    kind1 copy / kind2 core_stub / kind3 zero: addr > 0x80fdffff, addr+size <= 0x81000000
    kind1/2/4: fileoff+size <= total_len
    kind2: 8-byte stubs, every hash must be in the AP export table (else load fails)
Extra (ours): enter/exit inside the kind4 code region, no VFP/NEON instructions (needs ELF).
Optional: if $R05_ORIG_APP points at a dump of the stock app container (not shipped in
  this repo), every stub is also compared byte-for-byte with the stock stub for the same import.
"""
import struct, zlib, sys, csv, os, subprocess

BASE = 0x60270000
HERE = os.path.dirname(os.path.abspath(__file__))
CSV  = os.path.join(HERE, "r05_exports.csv")
ORIG = os.environ.get("R05_ORIG_APP")      # 可选: 原厂 app 容器 dump, 仓库里不带
ORIG_APP_SIZE = 0xa09a0      # 原厂 app 大小; 之后 0x60320000 起是资源区

def main():
    path = sys.argv[1]; elf = sys.argv[2] if len(sys.argv) > 2 else None
    d = open(path, "rb").read()
    ok = True
    def chk(c, msg):
        nonlocal ok
        print(("  OK  " if c else " FAIL ") + msg); ok = ok and bool(c)
    magic, total, crc, ver, enter, exit_, x18, x1c = struct.unpack_from("<8I", d, 0)
    chk(magic == 0x41505032, "magic 2PPA")
    chk(total == len(d) and total > 0x80, "total_len %d == file %d (>0x80)" % (total, len(d)))
    chk(BASE + total <= 0x60370000, "base+total <= 0x60370000 (loader)")
    chk(total <= ORIG_APP_SIZE, "total <= 原厂 app 大小 %#x (不压资源区)" % ORIG_APP_SIZE)
    z = bytearray(d); z[8:12] = b"\0\0\0\0"
    chk((zlib.crc32(bytes(z)) & 0xffffffff) == crc, "crc32@8 = %08x" % crc)
    chk(ver >> 16 == 1 and ver & 0xffff == 0, "version %#010x (= 原厂)" % ver)

    exp = {int(r["hash"], 16) for r in csv.DictReader(open(CSV))}
    orig_stub = {}
    if ORIG and os.path.exists(ORIG):
        o_ = open(ORIG, "rb").read()
        for i in range(0x638 // 8):
            b = o_[0x80 + 8*i: 0x88 + 8*i]
            orig_stub[struct.unpack_from("<I", b, 4)[0]] = b

    in_ram = lambda a, n=0: a > 0x80fdffff and a + n <= 0x81000000
    code = []; stubs = []
    for slot in range(6):
        k, fo, sz, ad = struct.unpack_from("<4I", d, 0x20 + 16*slot)
        if k == 0 or sz == 0: continue
        chk(1 <= k <= 4, "slot%d kind %d valid" % (slot, k))
        if k in (1, 2, 3):
            chk(in_ram(ad, sz), "slot%d kind%d RAM [%#x,+%#x) in app window" % (slot, k, ad, sz))
        if k in (1, 2, 4):
            chk(fo + sz <= total, "slot%d kind%d file range in-bounds" % (slot, k))
        if k == 4:
            chk(fo == ad - BASE, "slot%d kind4 XIP fileoff==addr-base" % slot); code.append((ad, sz))
        if k == 2:
            chk(sz % 8 == 0, "slot%d kind2 size multiple of 8" % slot); stubs.append((fo, sz))
    in_code = lambda a: any(s <= (a & ~1) < s + n for s, n in code)
    chk(enter and enter & 1 and in_code(enter), "enter %#x Thumb, in kind4 code" % enter)
    chk(exit_ & 1 and in_code(exit_), "exit  %#x Thumb, in kind4 code" % exit_)
    chk(x18 == 0 and x1c == 0, "@0x18/@0x1c = 0")

    n = bad = same = 0
    for fo, sz in stubs:
        for p in range(fo, fo + sz, 8):
            b = d[p:p+8]; h = struct.unpack_from("<I", b, 4)[0]; n += 1
            if b[:4] != bytes.fromhex("dff800f0") or h not in exp:
                bad += 1; print("  FAIL  stub @%#x %s not resolvable" % (p, b.hex()))
            elif h in orig_stub:
                if orig_stub[h] == b: same += 1
                else: bad += 1; print("  FAIL  stub @%#x differs from 原厂" % p)
    chk(bad == 0, "%d stubs resolve in R05 exports" % n
        + ("; %d byte-identical to 原厂 app stubs" % same if orig_stub else " (未提供 R05_ORIG_APP, 跳过逐字节比对)"))

    if elf:
        dis = subprocess.check_output(["arm-none-eabi-objdump", "-d", "-j", ".text", elf]).decode()
        vfp = [l for l in dis.splitlines() if "\t" in l and l.split("\t")[-1].startswith("v")]
        chk(not vfp, "no VFP/NEON instructions in .text" + ("" if not vfp else ": " + vfp[0]))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()

"""
convert_materials.py - Convert DXBC shaders to SPIRV in .material.bin files

Reads a .material.bin, finds all bgfx shader binaries containing DXBC bytecode,
converts them to SPIRV using dxil-spirv, and writes the modified file.

The bgfx binary format wraps the shader payload:
  [header bytes...] [u32 shaderSize] [DXBC payload] [trailing bytes...]
We replace the DXBC payload with SPIRV and update shaderSize.

Usage:
  python convert_materials.py <input_dir> <output_dir> [--dxil-spirv path] [--jobs N]
  python convert_materials.py single <input.material.bin> <output.material.bin>
"""

import struct
import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing


DXIL_SPIRV_EXE = None  # Set at runtime


def find_dxil_spirv():
    """Find dxil-spirv.exe relative to this script."""
    script_dir = Path(__file__).parent.parent
    candidates = [
        script_dir / "external" / "dxil-spirv" / "build3" / "Release" / "dxil-spirv.exe",
        script_dir / "external" / "dxil-spirv" / "build" / "Release" / "dxil-spirv.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    if shutil.which("dxil-spirv"):
        return "dxil-spirv"
    return None


def convert_dxbc_to_spirv(dxbc_data: bytes, exe_path: str) -> bytes:
    """Convert DXBC bytecode to SPIRV using dxil-spirv CLI."""
    with tempfile.NamedTemporaryFile(suffix=".dxbc", delete=False) as tmp_in:
        tmp_in.write(dxbc_data)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path + ".spv"

    try:
        result = subprocess.run(
            [exe_path, tmp_in_path, "--output", tmp_out_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace')
            raise RuntimeError(f"dxil-spirv failed: {stderr}")

        with open(tmp_out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_in_path)
        except OSError:
            pass
        try:
            os.unlink(tmp_out_path)
        except OSError:
            pass


def find_shader_size_field(data: bytes, dxbc_offset: int) -> int:
    """
    The shaderSize u32 is at (dxbc_offset - 4) and should equal the DXBC size.
    """
    if dxbc_offset < 4:
        return -1
    size_val = struct.unpack_from('<I', data, dxbc_offset - 4)[0]
    if dxbc_offset + 28 <= len(data):
        dxbc_total = struct.unpack_from('<I', data, dxbc_offset + 24)[0]
        if size_val == dxbc_total:
            return dxbc_offset - 4
    return -1


def find_bgfx_binary_size_field(data: bytes, dxbc_offset: int, dxbc_size: int) -> int:
    """
    Find the u32 bgfx_binary_size field that precedes the entire bgfx binary.
    The bgfx binary starts with VSH\\x05/FSH\\x05/CSH\\x05 magic.
    Search up to 2000 bytes back (large shaders have many uniforms in the header).

    We verify that the bgfx_size actually encompasses the DXBC blob to avoid
    matching a different nearby bgfx entry.
    """
    search_start = max(0, dxbc_offset - 2000)
    search_region = data[search_start:dxbc_offset]

    # Collect all candidates, pick the closest valid one
    candidates = []
    for magic in [b'VSH', b'FSH', b'CSH']:
        pos = len(search_region)
        while True:
            pos = search_region.rfind(magic, 0, pos)
            if pos == -1:
                break
            abs_pos = search_start + pos
            # Verify version byte is 5
            if abs_pos + 3 < len(data) and data[abs_pos + 3] == 5 and abs_pos >= 4:
                size_field_offset = abs_pos - 4
                bgfx_size = struct.unpack_from('<I', data, size_field_offset)[0]
                # The bgfx binary starts at abs_pos and must encompass the DXBC
                # DXBC ends at dxbc_offset + dxbc_size
                # bgfx binary ends at abs_pos + bgfx_size
                bgfx_end = abs_pos + bgfx_size
                dxbc_end = dxbc_offset + dxbc_size
                if bgfx_end >= dxbc_end:
                    candidates.append((abs_pos, size_field_offset))
            pos -= 1
            if pos < 0:
                break

    if not candidates:
        return -1
    # Pick the closest (highest offset) valid candidate
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def convert_material_bin(input_path: str, output_path: str, exe_path: str) -> dict:
    """
    Convert all DXBC shaders in a .material.bin to SPIRV.
    Returns stats dict.
    """
    with open(input_path, "rb") as f:
        data = bytearray(f.read())

    stats = {"total_dxbc": 0, "converted": 0, "failed": 0, "skipped": 0}

    # Find all DXBC blobs
    dxbc_locations = []
    pos = 0
    while True:
        idx = data.find(b'DXBC', pos)
        if idx == -1:
            break
        if idx + 28 <= len(data):
            dxbc_size = struct.unpack_from('<I', data, idx + 24)[0]
            dxbc_locations.append((idx, dxbc_size))
        pos = idx + 4

    stats["total_dxbc"] = len(dxbc_locations)

    if not dxbc_locations:
        shutil.copy2(input_path, output_path)
        return stats

    # Process in reverse order so earlier offsets stay valid
    for dxbc_offset, dxbc_size in reversed(dxbc_locations):
        if dxbc_offset + dxbc_size > len(data):
            stats["skipped"] += 1
            continue

        dxbc_data = bytes(data[dxbc_offset:dxbc_offset + dxbc_size])

        # Find the shaderSize field (4 bytes before DXBC, equals DXBC size)
        shader_size_offset = find_shader_size_field(data, dxbc_offset)
        if shader_size_offset == -1:
            stats["skipped"] += 1
            continue

        # Find the bgfx_binary_size field (before VSH/FSH/CSH header)
        # Some DXBC blobs (SM60/SM65/compute) aren't bgfx-wrapped — they have
        # just [u32 size][DXBC data] with no VSH/FSH/CSH header. In that case
        # shader_size_offset IS the only size field and there's no separate
        # bgfx size to update.
        bgfx_size_offset = find_bgfx_binary_size_field(data, dxbc_offset, dxbc_size)
        has_bgfx_wrapper = bgfx_size_offset != -1

        if has_bgfx_wrapper:
            old_bgfx_size = struct.unpack_from('<I', data, bgfx_size_offset)[0]

        try:
            spirv_data = convert_dxbc_to_spirv(dxbc_data, exe_path)
        except Exception as e:
            print(f"  ERROR converting DXBC at {dxbc_offset:#x}: {e}", flush=True)
            stats["failed"] += 1
            continue

        size_diff = len(spirv_data) - dxbc_size
        data[dxbc_offset:dxbc_offset + dxbc_size] = spirv_data
        struct.pack_into('<I', data, shader_size_offset, len(spirv_data))

        if has_bgfx_wrapper:
            new_bgfx_size = old_bgfx_size + size_diff
            struct.pack_into('<I', data, bgfx_size_offset, new_bgfx_size)

        stats["converted"] += 1

    with open(output_path, "wb") as f:
        f.write(data)

    return stats


def _convert_worker(args):
    """Worker function for parallel file conversion."""
    input_path, output_path, exe_path = args
    fname = os.path.basename(input_path)
    try:
        stats = convert_material_bin(input_path, output_path, exe_path)
        return fname, stats, None
    except Exception as e:
        shutil.copy2(input_path, output_path)
        return fname, None, str(e)


def main():
    global DXIL_SPIRV_EXE

    if len(sys.argv) < 3:
        print("Usage:")
        print("  python convert_materials.py <input_dir> <output_dir> [--dxil-spirv path] [--jobs N]")
        print("  python convert_materials.py single <input.material.bin> <output.material.bin>")
        sys.exit(1)

    # Parse flags
    dxil_spirv_path = None
    num_jobs = max(1, multiprocessing.cpu_count() // 2)
    args = list(sys.argv[1:])

    if "--dxil-spirv" in args:
        idx = args.index("--dxil-spirv")
        dxil_spirv_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if "--jobs" in args:
        idx = args.index("--jobs")
        num_jobs = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    DXIL_SPIRV_EXE = dxil_spirv_path or find_dxil_spirv()
    if not DXIL_SPIRV_EXE:
        print("ERROR: dxil-spirv.exe not found. Build it or pass --dxil-spirv <path>")
        sys.exit(1)
    print(f"Using dxil-spirv: {DXIL_SPIRV_EXE}", flush=True)

    if args[0] == "single":
        if len(args) < 3:
            print("Usage: python convert_materials.py single <input> <output>")
            sys.exit(1)
        input_path = args[1]
        output_path = args[2]
        print(f"Converting {input_path}...", flush=True)
        stats = convert_material_bin(input_path, output_path, DXIL_SPIRV_EXE)
        print(f"Done: {stats['converted']}/{stats['total_dxbc']} converted, "
              f"{stats['failed']} failed, {stats['skipped']} skipped")
    else:
        input_dir = args[0]
        output_dir = args[1]
        os.makedirs(output_dir, exist_ok=True)

        files = sorted(f for f in os.listdir(input_dir) if f.endswith('.material.bin'))
        print(f"Found {len(files)} material files, using {num_jobs} workers", flush=True)

        work_items = [
            (os.path.join(input_dir, f), os.path.join(output_dir, f), DXIL_SPIRV_EXE)
            for f in files
        ]

        total_stats = {"total_dxbc": 0, "converted": 0, "failed": 0, "skipped": 0}
        done = 0

        with ProcessPoolExecutor(max_workers=num_jobs) as pool:
            futures = {pool.submit(_convert_worker, item): item for item in work_items}
            for future in as_completed(futures):
                done += 1
                fname, stats, error = future.result()
                if error:
                    print(f"[{done}/{len(files)}] {fname} ERROR: {error}", flush=True)
                else:
                    for k in total_stats:
                        total_stats[k] += stats[k]
                    print(f"[{done}/{len(files)}] {fname}: "
                          f"{stats['converted']}/{stats['total_dxbc']} converted", flush=True)

        print(f"\n=== Summary ===")
        print(f"Total DXBC shaders: {total_stats['total_dxbc']}")
        print(f"Converted: {total_stats['converted']}")
        print(f"Failed: {total_stats['failed']}")
        print(f"Skipped: {total_stats['skipped']}")


if __name__ == "__main__":
    main()

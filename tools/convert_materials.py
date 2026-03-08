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


def downgrade_spirv(spirv_data: bytes) -> bytes:
    """
    Patch SPIRV to be compatible with bgfx's Vulkan 1.0/1.1 backend.

    dxil-spirv emits SPIRV 1.6 with capabilities bgfx doesn't support:
      - VulkanMemoryModel / VulkanMemoryModelDeviceScope
      - DemoteToHelperInvocation / DemoteToHelperInvocationEXT
      - FragmentShadingRateKHR
      - OpMemoryModel Logical Vulkan (must be GLSL450)

    We patch in-place: NOP out unsupported capabilities/extensions,
    fix the memory model, downgrade version to 1.3, and replace
    OpDemoteToHelperInvocation with OpKill.
    """
    if len(spirv_data) < 20:
        return spirv_data
    words = list(struct.unpack_from(f'<{len(spirv_data)//4}I', spirv_data))

    # Word 0: magic, Word 1: version -> set to SPIRV 1.3 (Vulkan 1.1)
    words[1] = 0x00010300

    UNSUPPORTED_CAPS = {
        4427,  # VulkanMemoryModel
        4428,  # VulkanMemoryModelDeviceScope
        4465,  # DemoteToHelperInvocation (SPIRV 1.6)
        4466,  # DemoteToHelperInvocationEXT
        5345,  # FragmentShadingRateKHR
    }

    i = 5  # skip 5-word header
    while i < len(words):
        wcount = (words[i] >> 16) & 0xFFFF
        opcode = words[i] & 0xFFFF
        if wcount == 0:
            break

        if opcode == 17 and wcount == 2:  # OpCapability
            cap = words[i + 1]
            if cap in UNSUPPORTED_CAPS:
                # Replace with OpNop (opcode 0, wordcount 1)
                words[i] = 0x00010000
                words[i + 1] = 0x00010000  # another OpNop

        elif opcode == 11:  # OpExtension
            # Read extension name from the word payload
            name_bytes = b''
            for j in range(1, wcount):
                name_bytes += struct.pack('<I', words[i + j])
            name = name_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
            if any(ext in name for ext in [
                'vulkan_memory_model', 'shader_demote', 'fragment_shading_rate'
            ]):
                for j in range(wcount):
                    words[i + j] = 0x00010000  # OpNop

        elif opcode == 14 and wcount == 3:  # OpMemoryModel
            # words[i+1] = addressing model (0=Logical), words[i+2] = memory model
            if words[i + 2] == 3:  # Vulkan -> GLSL450
                words[i + 2] = 1

        elif opcode == 5765:  # OpDemoteToHelperInvocation
            # Replace with OpKill (opcode 252, wcount 1)
            words[i] = (1 << 16) | 252
            for j in range(1, wcount):
                words[i + j] = 0x00010000

        elif opcode == 15:  # OpEntryPoint - past preamble, can stop checking caps
            pass  # keep scanning for OpDemoteToHelperInvocation in function body

        i += wcount

    return struct.pack(f'<{len(words)}I', *words)


# bgfx::Attrib enum — maps attribute name to Vulkan vertex input location
BGFX_ATTRIB = {
    'position': 0, 'normal': 1, 'tangent': 2, 'bitangent': 3,
    'color0': 4, 'color1': 5, 'color2': 6, 'color3': 7,
    'indices': 8, 'weight': 9,
    'texcoord0': 10, 'texcoord1': 11, 'texcoord2': 12, 'texcoord3': 13,
    'texcoord4': 14, 'texcoord5': 15, 'texcoord6': 16, 'texcoord7': 17,
    'texcoord8': 18,
}


def remap_spirv_locations(spirv_data: bytes, location_map: dict) -> tuple:
    """
    Patch SPIRV vertex input Location decorations to match bgfx::Attrib enum.

    In bgfx's Vulkan renderer, vertex attribute locations = bgfx::Attrib values
    (e.g. Position=0, TexCoord0=10). But dxil-spirv assigns sequential locations
    (0, 1, 2...) based on DXBC register order. This function remaps them.

    location_map: {old_location: new_location}
    Returns: (patched_spirv_bytes, num_patched)
    """
    if len(spirv_data) < 20 or not location_map:
        return spirv_data, 0

    words = list(struct.unpack_from(f'<{len(spirv_data)//4}I', spirv_data))

    # Pass 1: find all Input variables (StorageClass = 1)
    input_ids = set()
    i = 5
    while i < len(words):
        wcount = (words[i] >> 16) & 0xFFFF
        opcode = words[i] & 0xFFFF
        if wcount == 0:
            break
        if opcode == 59 and wcount >= 4:  # OpVariable
            storage_class = words[i + 3]
            if storage_class == 1:  # Input
                input_ids.add(words[i + 2])
        i += wcount

    # Pass 2: patch Location decorations for Input variables
    patched = 0
    i = 5
    while i < len(words):
        wcount = (words[i] >> 16) & 0xFFFF
        opcode = words[i] & 0xFFFF
        if wcount == 0:
            break
        if opcode == 71 and wcount >= 4:  # OpDecorate
            target = words[i + 1]
            decoration = words[i + 2]
            if decoration == 30 and target in input_ids:  # Location
                old_loc = words[i + 3]
                if old_loc in location_map:
                    words[i + 3] = location_map[old_loc]
                    patched += 1
        i += wcount

    return struct.pack(f'<{len(words)}I', *words), patched


def fixlocs_material(data: bytearray) -> int:
    """
    Fix SPIRV vertex input locations in a converted .material.bin.

    Scans for vertex shader entries, reads their ShaderInput names,
    builds a location remap from sequential → bgfx::Attrib, and patches SPIRV.

    Returns total number of SPIRV locations patched.
    """
    total_patched = 0

    # Find all vertex shader entries by scanning for len-prefixed "Vertex" string
    vertex_needle = struct.pack('<I', 6) + b'Vertex'
    pos = 0
    while True:
        idx = data.find(vertex_needle, pos)
        if idx == -1:
            break
        pos = idx + len(vertex_needle)

        # Read platform string
        try:
            plat_len = struct.unpack_from('<I', data, pos)[0]
            if plat_len > 100:
                continue
            platform = data[pos+4:pos+4+plat_len].decode('utf-8', errors='replace')
            pos = pos + 4 + plat_len

            # stage_id (u8), platform_id (u8)
            pos += 2

            # input_count (u16)
            input_count = struct.unpack_from('<H', data, pos)[0]
            pos += 2
            if input_count > 32:
                continue

            # Read ShaderInput entries
            input_names = []
            for _ in range(input_count):
                nlen = struct.unpack_from('<I', data, pos)[0]; pos += 4
                name = data[pos:pos+nlen].decode('utf-8', errors='replace'); pos += nlen
                pos += 6  # type_id(1) + attr_idx(1) + unk0(2) + unk1(2)
                input_names.append(name.lower())

            # hash (u64)
            pos += 8

            # bgfx_binary_size (u32)
            bgfx_size = struct.unpack_from('<I', data, pos)[0]; pos += 4
            bgfx_start = pos

            # Check if this is a VSH with SPIRV
            if bgfx_size < 14 or bgfx_size > 10_000_000:
                continue
            if data[bgfx_start:bgfx_start+3] != b'VSH':
                continue

            # Parse bgfx header to find SPIRV offset
            hpos = bgfx_start + 4  # skip magic
            hpos += 4  # hash
            uni_count = struct.unpack_from('<H', data, hpos)[0]; hpos += 2
            for _ in range(uni_count):
                nlen = data[hpos]; hpos += 1
                hpos += nlen  # name
                hpos += 7  # type(1)+num(1)+reg(2)+cnt(2)+??  -- actually 6: type(1)+num(1)+regIdx(2)+regCnt(2)
            # Wait, let me use the correct 6 bytes based on our analysis
        except (struct.error, IndexError):
            continue

        # Re-parse bgfx header more carefully
        try:
            hpos = bgfx_start + 4 + 4 + 2  # magic + hash + uniformCount
            for _ in range(uni_count):
                nlen = data[hpos]; hpos += 1
                hpos += nlen + 6  # name + type(1)+num(1)+regIdx(2)+regCnt(2)
            shader_size = struct.unpack_from('<I', data, hpos)[0]; hpos += 4
            spirv_offset = hpos

            # Check for SPIRV magic
            if data[spirv_offset:spirv_offset+4] != b'\x03\x02\x23\x07':
                continue

            # Build location map from input names
            location_map = {}
            for i, name in enumerate(input_names):
                if name in BGFX_ATTRIB:
                    bgfx_loc = BGFX_ATTRIB[name]
                    if i != bgfx_loc:
                        location_map[i] = bgfx_loc

            if not location_map:
                continue

            # Extract SPIRV, remap, and write back
            spirv_data = bytes(data[spirv_offset:spirv_offset + shader_size])
            patched_spirv, count = remap_spirv_locations(spirv_data, location_map)
            if count > 0:
                data[spirv_offset:spirv_offset + shader_size] = patched_spirv
                total_patched += count
        except (struct.error, IndexError):
            continue

    return total_patched


def remap_spirv_bindings(spirv_data: bytes, stage_base: int) -> tuple:
    """
    Patch SPIRV descriptor bindings to match bgfx Vulkan binding scheme.

    bgfx Vulkan binding layout per stage (stage_base = 0 for VS, 48 for FS):
      - binding stage_base+0:  UBO (UNIFORM_BUFFER_DYNAMIC)
      - binding stage_base+16+N: SAMPLED_IMAGE for sampler slot N
      - binding stage_base+32+N: SAMPLER for sampler slot N

    dxil-spirv outputs separate OpTypeImage and OpTypeSampler variables at
    binding N (from D3D t/s register N), plus UBO at binding 0 or 1.
    We remap these to bgfx's scheme.

    Returns: (patched_spirv_bytes, num_patched)
    """
    if len(spirv_data) < 20:
        return spirv_data, 0

    words = list(struct.unpack_from(f'<{len(spirv_data)//4}I', spirv_data))

    # Pass 1: collect type info, variables, and binding decorations
    base_types = {}       # type_id -> 'Image' | 'Sampler' | 'Struct' | ...
    pointer_pointees = {} # pointer_type_id -> pointee_type_id
    var_info = {}         # var_id -> (storage_class, type_id)
    binding_locs = {}     # var_id -> word index of binding value in words[]
    bindings = {}         # var_id -> current binding value

    i = 5
    while i < len(words):
        wc = (words[i] >> 16) & 0xFFFF
        op = words[i] & 0xFFFF
        if wc == 0:
            break
        if op == 25:    # OpTypeImage
            base_types[words[i+1]] = 'Image'
        elif op == 26:  # OpTypeSampler
            base_types[words[i+1]] = 'Sampler'
        elif op == 27:  # OpTypeSampledImage
            base_types[words[i+1]] = 'SampledImage'
        elif op == 30:  # OpTypeStruct
            base_types[words[i+1]] = 'Struct'
        elif op == 28 and wc >= 3:  # OpTypeArray
            base_types[words[i+1]] = base_types.get(words[i+2], 'Unknown')
        elif op == 29 and wc >= 2:  # OpTypeRuntimeArray
            base_types[words[i+1]] = base_types.get(words[i+2], 'Unknown')
        elif op == 32 and wc >= 4:  # OpTypePointer
            pointer_pointees[words[i+1]] = words[i+3]
        elif op == 59 and wc >= 4:  # OpVariable
            var_info[words[i+2]] = (words[i+3], words[i+1])
        elif op == 71 and wc >= 4:  # OpDecorate
            if words[i+2] == 33:  # Binding
                bindings[words[i+1]] = words[i+3]
                binding_locs[words[i+1]] = i + 3
        i += wc

    # Pass 2: remap bindings based on variable type
    patched = 0
    for var_id, (sc, tid) in var_info.items():
        if var_id not in binding_locs:
            continue

        old_binding = bindings[var_id]

        # Resolve pointer -> pointee type
        pointee_id = pointer_pointees.get(tid, tid)
        var_type = base_types.get(pointee_id, 'Unknown')

        if sc == 2:  # Uniform storage class -> UBO
            new_binding = stage_base
        elif sc == 0:  # UniformConstant -> texture or sampler
            if var_type == 'Image' or var_type == 'SampledImage':
                if old_binding >= stage_base + 16:  # already in Vulkan range
                    continue
                new_binding = stage_base + 16 + old_binding
            elif var_type == 'Sampler':
                if old_binding >= stage_base + 32:  # already in Vulkan range
                    continue
                new_binding = stage_base + 32 + old_binding
            else:
                continue
        else:
            continue

        if new_binding != old_binding:
            words[binding_locs[var_id]] = new_binding
            patched += 1

    return struct.pack(f'<{len(words)}I', *words), patched


def fixbindings_material(data: bytearray) -> int:
    """
    Fix SPIRV descriptor bindings and bgfx header sampler reg values
    in a converted .material.bin to match bgfx's Vulkan binding scheme.

    Idempotent: detects already-fixed shaders by checking if sampler regIdx
    values are already in the Vulkan range (>= stage_base + 16). D3D original
    sampler registers are small numbers (0, 1, 2...) so this is reliable.

    For each VSH/FSH shader:
      - Rewrites sampler uniform header entries (regIdx, regCnt) to Vulkan bindings
      - Patches SPIRV binding decorations to match

    Returns total number of items patched.
    """
    total_patched = 0

    pos = 0
    while pos < len(data) - 10:
        # Find next shader header
        vsh = data.find(b'VSH\x05', pos)
        fsh = data.find(b'FSH\x05', pos)

        if vsh == -1 and fsh == -1:
            break
        if vsh == -1: vsh = len(data)
        if fsh == -1: fsh = len(data)

        shader_start = min(vsh, fsh)
        is_fs = (shader_start == fsh)
        stage_base = 48 if is_fs else 0

        try:
            hpos = shader_start + 4  # skip magic+version
            hpos += 4  # skip hash
            num_uniforms = struct.unpack_from('<H', data, hpos)[0]
            hpos += 2

            # First pass: check if already fixed by looking at sampler regIdx
            already_fixed = False
            sampler_offsets = []  # (reg_idx_offset, reg_cnt_offset, reg_idx)
            scan_pos = hpos
            for _ in range(num_uniforms):
                name_len = data[scan_pos]; scan_pos += 1
                scan_pos += name_len
                utype = data[scan_pos]; scan_pos += 1
                scan_pos += 1  # num
                ri_off = scan_pos
                ri = struct.unpack_from('<H', data, scan_pos)[0]; scan_pos += 2
                rc_off = scan_pos
                scan_pos += 2
                if (utype & 0xCF) == 0:  # Sampler
                    sampler_offsets.append((ri_off, rc_off, ri))
                    if ri >= stage_base + 16:
                        already_fixed = True

            hpos = scan_pos  # advance past uniforms

            if not already_fixed:
                # Rewrite sampler entries
                for ri_off, rc_off, ri in sampler_offsets:
                    struct.pack_into('<H', data, ri_off, stage_base + 16 + ri)
                    struct.pack_into('<H', data, rc_off, stage_base + 32 + ri)
                    total_patched += 1

            # Read shader size and find SPIRV
            shader_size = struct.unpack_from('<I', data, hpos)[0]
            hpos += 4
            spirv_start = hpos

            if (not already_fixed and spirv_start + 4 <= len(data) and
                    data[spirv_start:spirv_start+4] == b'\x03\x02\x23\x07'):
                spirv_data = bytes(data[spirv_start:spirv_start+shader_size])
                patched_spirv, count = remap_spirv_bindings(spirv_data, stage_base)
                if count > 0:
                    data[spirv_start:spirv_start+shader_size] = patched_spirv
                    total_patched += count

        except (struct.error, IndexError):
            pass

        pos = shader_start + 4

    return total_patched


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


def retag_platforms(data: bytearray, target_platform="Vulkan", target_id=11) -> int:
    """
    Replace all Direct3D_SM* / Direct3D_X* platform strings with target platform
    in a .material.bin bytearray. Also updates the platform_id byte.
    Returns the number of entries retagged.
    """
    platforms = [
        b'Direct3D_SM40',  # 13 chars
        b'Direct3D_SM50',  # 13 chars
        b'Direct3D_SM60',  # 13 chars
        b'Direct3D_SM65',  # 13 chars
        b'Direct3D_XB1',   # 12 chars
        b'Direct3D_XBX',   # 12 chars
    ]

    target_bytes = target_platform.encode('utf-8')
    target_needle = struct.pack('<I', len(target_bytes)) + target_bytes

    # Find all occurrences of length-prefixed platform strings
    replacements = []
    for ps in platforms:
        needle = struct.pack('<I', len(ps)) + ps
        pos = 0
        while True:
            idx = data.find(needle, pos)
            if idx == -1:
                break
            replacements.append((idx, len(needle)))
            pos = idx + len(needle)

    # Sort by offset descending so earlier offsets stay valid
    replacements.sort(key=lambda x: x[0], reverse=True)

    for idx, old_len in replacements:
        # Replace length-prefixed platform string
        data[idx:idx + old_len] = target_needle
        # After the platform string: stage_id (1 byte), then platform_id (1 byte)
        pid_offset = idx + len(target_needle) + 1
        if pid_offset < len(data):
            data[pid_offset] = target_id

    return len(replacements)


def convert_material_bin(input_path: str, output_path: str, exe_path: str) -> dict:
    """
    Convert all DXBC shaders in a .material.bin to SPIRV.
    Returns stats dict.
    """
    with open(input_path, "rb") as f:
        data = bytearray(f.read())

    stats = {"total_dxbc": 0, "converted": 0, "failed": 0, "skipped": 0, "retagged": 0}

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
        # Still retag platforms even if no DXBC (already converted files)
        retagged = retag_platforms(data)
        stats["retagged"] = retagged
        with open(output_path, "wb") as f:
            f.write(data)
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
            spirv_data = downgrade_spirv(spirv_data)
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

    # Re-tag platform strings so Vulkan renderer finds matching entries
    retagged = retag_platforms(data)
    stats["retagged"] = retagged

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
        print("  python convert_materials.py retag <dir>  -- retag platform strings in already-converted files")
        print("  python convert_materials.py fixlocs <dir> -- fix SPIRV vertex input locations for bgfx")
        print("  python convert_materials.py fixbindings <dir> -- fix SPIRV descriptor bindings for bgfx Vulkan")
        sys.exit(1)

    args = list(sys.argv[1:])

    # fixlocs doesn't need dxil-spirv, handle early
    if args[0] == "fixlocs":
        target_dir = args[1]
        files = sorted(f for f in os.listdir(target_dir) if f.endswith('.material.bin'))
        print(f"Fixing SPIRV vertex input locations in {len(files)} files", flush=True)
        total_patched = 0
        for fname in files:
            fpath = os.path.join(target_dir, fname)
            with open(fpath, "rb") as f:
                data = bytearray(f.read())
            count = fixlocs_material(data)
            if count > 0:
                with open(fpath, "wb") as f:
                    f.write(data)
            total_patched += count
            if count > 0:
                print(f"  {fname}: {count} locations remapped", flush=True)
        print(f"\nTotal: {total_patched} locations remapped in {len(files)} files")
        return

    if args[0] == "fixbindings":
        target_dir = args[1]
        files = sorted(f for f in os.listdir(target_dir) if f.endswith('.material.bin'))
        print(f"Fixing SPIRV descriptor bindings in {len(files)} files", flush=True)
        total_patched = 0
        for fname in files:
            fpath = os.path.join(target_dir, fname)
            with open(fpath, "rb") as f:
                data = bytearray(f.read())
            count = fixbindings_material(data)
            if count > 0:
                with open(fpath, "wb") as f:
                    f.write(data)
            total_patched += count
            if count > 0:
                print(f"  {fname}: {count} bindings remapped", flush=True)
        print(f"\nTotal: {total_patched} bindings remapped in {len(files)} files")
        return

    # Parse flags
    dxil_spirv_path = None
    num_jobs = max(1, multiprocessing.cpu_count() // 2)

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

    if args[0] == "retag":
        target_dir = args[1]
        files = sorted(f for f in os.listdir(target_dir) if f.endswith('.material.bin'))
        print(f"Retagging {len(files)} material files in {target_dir}", flush=True)
        total_retagged = 0
        for fname in files:
            fpath = os.path.join(target_dir, fname)
            with open(fpath, "rb") as f:
                data = bytearray(f.read())
            count = retag_platforms(data)
            if count > 0:
                with open(fpath, "wb") as f:
                    f.write(data)
            total_retagged += count
            print(f"  {fname}: {count} entries retagged", flush=True)
        print(f"\nTotal: {total_retagged} entries retagged in {len(files)} files")
        return

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

        total_stats = {"total_dxbc": 0, "converted": 0, "failed": 0, "skipped": 0, "retagged": 0}
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
        print(f"Retagged: {total_stats['retagged']} (Direct3D_SM* -> Vulkan)")


if __name__ == "__main__":
    main()

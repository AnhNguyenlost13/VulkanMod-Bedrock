"""
material_bin.py - Parser/writer for Minecraft Bedrock .material.bin files
Based on reverse engineering + MaterialBinTool format spec

Format (version 22, unencrypted):
  Header:
    u32 magic = 0x0A11DA1A
    u32 padding = 0
    str class_name = "RenderDragon.CompiledMaterialDefinition"  (len-prefixed u32)
    u32 version = 22
    u32 encryption_variant = 0
    4cc encryption = "ENON" (NONE reversed)
    str material_name (len-prefixed u32)

  Then: sampler definitions, property fields, pass definitions with shader code entries
  Each shader code entry has:
    str stage ("Vertex", "Fragment", "Compute")
    str platform ("Direct3D_SM40", "Direct3D_SM50", etc.)
    u8  stage_id
    u8  platform_id
    u16 input_count
    [inputs]
    u64 hash
    bytes bgfx_shader_binary  (VSH/FSH/CSH header + DXBC/SPIRV payload)

  Footer:
    u32 magic = 0x0A11DA1A (repeated)
    u32 padding = 0
"""

import struct
import io
from dataclasses import dataclass, field
from typing import List, Optional


MAGIC = 0x0A11DA1A
CLASS_NAME = "RenderDragon.CompiledMaterialDefinition"
VERSION = 22


def read_u8(f) -> int:
    return struct.unpack('<B', f.read(1))[0]

def read_u16(f) -> int:
    return struct.unpack('<H', f.read(2))[0]

def read_u32(f) -> int:
    return struct.unpack('<I', f.read(4))[0]

def read_u64(f) -> int:
    return struct.unpack('<Q', f.read(8))[0]

def read_i32(f) -> int:
    return struct.unpack('<i', f.read(4))[0]

def read_f32(f) -> float:
    return struct.unpack('<f', f.read(4))[0]

def read_string(f) -> str:
    length = read_u32(f)
    return f.read(length).decode('utf-8')

def read_bytes(f, n) -> bytes:
    return f.read(n)


def write_u8(f, v):
    f.write(struct.pack('<B', v))

def write_u16(f, v):
    f.write(struct.pack('<H', v))

def write_u32(f, v):
    f.write(struct.pack('<I', v))

def write_u64(f, v):
    f.write(struct.pack('<Q', v))

def write_i32(f, v):
    f.write(struct.pack('<i', v))

def write_f32(f, v):
    f.write(struct.pack('<f', v))

def write_string(f, s: str):
    data = s.encode('utf-8')
    write_u32(f, len(data))
    f.write(data)

def write_bytes(f, data: bytes):
    f.write(data)


@dataclass
class ShaderInput:
    name: str
    type_id: int  # u8
    attribute_index: int  # u8 - for vertex attribs
    unknown0: int  # u16
    unknown1: int  # u16

    @staticmethod
    def read(f) -> 'ShaderInput':
        name = read_string(f)
        type_id = read_u8(f)
        attribute_index = read_u8(f)
        unknown0 = read_u16(f)
        unknown1 = read_u16(f)
        return ShaderInput(name, type_id, attribute_index, unknown0, unknown1)

    def write(self, f):
        write_string(f, self.name)
        write_u8(f, self.type_id)
        write_u8(f, self.attribute_index)
        write_u16(f, self.unknown0)
        write_u16(f, self.unknown1)


@dataclass
class BgfxShaderBinary:
    """Raw bgfx shader binary (VSH\x05 / FSH\x05 / CSH\x05 header + payload)"""
    raw_data: bytes

    @property
    def magic(self) -> str:
        if len(self.raw_data) >= 3:
            return self.raw_data[:3].decode('ascii', errors='replace')
        return ""

    @property
    def version(self) -> int:
        if len(self.raw_data) >= 4:
            return self.raw_data[3]
        return 0

    def extract_dxbc(self) -> Optional[bytes]:
        """Extract DXBC bytecode from bgfx binary"""
        idx = self.raw_data.find(b'DXBC')
        if idx == -1:
            return None
        # DXBC header: magic(4) + hash(16) + version(4) + size(4)
        if idx + 24 > len(self.raw_data):
            return None
        total_size = struct.unpack_from('<I', self.raw_data, idx + 20)[0]
        return self.raw_data[idx:idx + total_size]

    def has_dxbc(self) -> bool:
        return b'DXBC' in self.raw_data

    def has_spirv(self) -> bool:
        # SPIRV magic: 0x07230203
        return b'\x03\x02\x23\x07' in self.raw_data


@dataclass
class ShaderCode:
    stage: str          # "Vertex", "Fragment", "Compute"
    platform: str       # "Direct3D_SM40", "Direct3D_SM50", etc.
    stage_id: int       # u8
    platform_id: int    # u8
    inputs: List[ShaderInput]
    hash_value: int     # u64 (8 bytes)
    bgfx_binary: BgfxShaderBinary

    @staticmethod
    def read(f) -> 'ShaderCode':
        stage = read_string(f)
        platform = read_string(f)
        stage_id = read_u8(f)
        platform_id = read_u8(f)
        input_count = read_u16(f)
        inputs = [ShaderInput.read(f) for _ in range(input_count)]
        hash_value = read_u64(f)

        # Read bgfx shader binary
        # The binary is length-prefixed
        bgfx_size = read_u32(f)
        bgfx_data = read_bytes(f, bgfx_size)
        bgfx_binary = BgfxShaderBinary(bgfx_data)

        return ShaderCode(stage, platform, stage_id, platform_id,
                         inputs, hash_value, bgfx_binary)

    def write(self, f):
        write_string(f, self.stage)
        write_string(f, self.platform)
        write_u8(f, self.stage_id)
        write_u8(f, self.platform_id)
        write_u16(f, len(self.inputs))
        for inp in self.inputs:
            inp.write(f)
        write_u64(f, self.hash_value)
        write_u32(f, len(self.bgfx_binary.raw_data))
        write_bytes(f, self.bgfx_binary.raw_data)


def scan_shader_codes(data: bytes) -> List[dict]:
    """
    Scan a .material.bin file for shader code entries by finding
    platform string patterns. Returns offsets and metadata.
    """
    results = []
    platform_strings = [
        b'Direct3D_SM40', b'Direct3D_SM50',
        b'Direct3D_SM60', b'Direct3D_SM65',
        b'Direct3D_XB1', b'Direct3D_XBX',
        b'GLSL_120', b'GLSL_430',
        b'ESSL_300', b'ESSL_310',
    ]

    for ps in platform_strings:
        start = 0
        while True:
            # Find length-prefixed platform string
            needle = struct.pack('<I', len(ps)) + ps
            idx = data.find(needle, start)
            if idx == -1:
                break

            # The stage string should be right before the platform string
            # (also length-prefixed)
            results.append({
                'platform': ps.decode(),
                'platform_offset': idx,
            })
            start = idx + len(needle)

    return sorted(results, key=lambda x: x['platform_offset'])


def scan_dxbc_blobs(data: bytes) -> List[dict]:
    """Find all DXBC shader blobs in binary data."""
    results = []
    start = 0
    while True:
        idx = data.find(b'DXBC', start)
        if idx == -1:
            break
        if idx + 28 <= len(data):
            total_size = struct.unpack_from('<I', data, idx + 24)[0]
            results.append({
                'offset': idx,
                'size': total_size,
                'data': data[idx:idx + total_size] if idx + total_size <= len(data) else None,
            })
        start = idx + 4
    return results


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python material_bin.py <file.material.bin>")
        sys.exit(1)

    filepath = sys.argv[1]
    with open(filepath, 'rb') as f:
        data = f.read()

    # Parse header
    buf = io.BytesIO(data)
    magic = read_u32(buf)
    assert magic == MAGIC, f"Bad magic: {magic:#x}"
    padding = read_u32(buf)
    class_name = read_string(buf)
    print(f"Class: {class_name}")
    version = read_u32(buf)
    print(f"Version: {version}")
    enc_variant = read_u32(buf)
    enc_tag = buf.read(4)
    print(f"Encryption: {enc_tag[::-1].decode()} (variant {enc_variant})")
    mat_name = read_string(buf)
    print(f"Material: {mat_name}")

    # Scan for shader platform entries
    print(f"\n--- Shader Platform Entries ---")
    entries = scan_shader_codes(data)
    for e in entries:
        print(f"  offset {e['platform_offset']:#x}: {e['platform']}")

    # Scan for DXBC blobs
    print(f"\n--- DXBC Blobs ---")
    blobs = scan_dxbc_blobs(data)
    for b in blobs:
        print(f"  offset {b['offset']:#x}: {b['size']} bytes")

    print(f"\nTotal: {len(entries)} shader entries, {len(blobs)} DXBC blobs")
    print(f"File size: {len(data)} bytes")

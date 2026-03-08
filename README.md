# FuckDX

Force Minecraft Bedrock Edition (Windows, 1.21.132) to use the Vulkan rendering backend instead of DirectX.

Minecraft Bedrock uses bgfx with RenderDragon and defaults to Direct3D 12 on Windows. This project forces the Vulkan renderer by:

1. **Hooking `bgfx::init`** to override the renderer type to Vulkan
2. **Patching the renderer type global** so the material system still selects DirectX shader entries (which now contain pre-converted SPIRV bytecode)
3. **Pre-converting all `.material.bin` files** from DXBC to SPIRV offline

## Prerequisites

- Windows 10/11
- Minecraft Bedrock 1.21.132 (Windows GDK / UWP)
- CMake 3.20+
- Visual Studio 2026 (MSVC v19.50+)
- Python 3.10+ (for material conversion)
- A Vulkan-capable GPU with up-to-date drivers

## Building

### DLL (injector mod)

```bash
cmake -S . -B build -G "Visual Studio 18 2026" -A x64
cmake --build build --config Release
```

This produces `build/Release/FuckDX.dll`.

### dxil-spirv (shader converter)

```bash
cd external/dxil-spirv
git submodule update --init --recursive
cmake -S . -B build3 -G "Visual Studio 18 2026" -A x64
cmake --build build3 --config Release --target dxil-spirv
```

This produces `external/dxil-spirv/build3/Release/dxil-spirv.exe`.

## Usage

### 1. Convert material files

Convert all `.material.bin` files from DXBC to SPIRV:

```bash
python tools/convert_materials.py <materials_dir> <output_dir> [--jobs N]
```

Example:
```bash
python tools/convert_materials.py \
  "C:\path\to\Minecraft\data\renderer\materials" \
  "converted_materials" \
  --jobs 8
```

Or convert a single file:
```bash
python tools/convert_materials.py single input.material.bin output.material.bin
```

### 2. Replace material files

Copy the converted `.material.bin` files back into the game's `data/renderer/materials/` directory, replacing the originals.

### 3. Inject the DLL

Inject `FuckDX.dll` into `Minecraft.Windows.exe` at startup using your preferred DLL injector. The DLL will:

- Hook `bgfx::init` and force Vulkan renderer
- Patch the renderer type global to D3D12 so the material system loads the "Direct3D_SM50" shader entries (which now contain SPIRV)
- Open a console window showing status messages

## How it works

### The problem

Minecraft Bedrock's `.material.bin` files contain compiled shaders tagged by platform (e.g. `Direct3D_SM50`). There are no Vulkan/SPIRV shader entries. When you force Vulkan, the material system can't find matching shaders and the GPU hangs (TDR).

### The solution

1. **Offline**: Convert DXBC bytecode inside bgfx shader binaries to SPIRV using [dxil-spirv](https://github.com/HansKristian-Work/dxil-spirv). The shader entries keep their `Direct3D_SM50` platform tag, but the payload is now SPIRV.

2. **Runtime**: The DLL hooks `bgfx::init` to force Vulkan. After init completes, it patches `dword_14970B170` (the renderer type global, RVA `0x970B170`) back to D3D12 (2). This tricks the material system into selecting DirectX-tagged entries, which now contain SPIRV that Vulkan can consume.

### Key addresses (1.21.132)

| Address (VA) | RVA | Description |
|---|---|---|
| `0x146ADD160` | `0x6ADD160` | `bgfx::init` (hook target) |
| `0x14970B170` | `0x970B170` | Renderer type global |
| `0x146AD8940` | `0x6AD8940` | `bgfx::Context::_initFinalize` |

## Project structure

```
FuckDX/
  src/main.cpp              # DLL source (hook + patches)
  CMakeLists.txt            # DLL build
  tools/
    convert_materials.py    # Batch DXBC->SPIRV material converter
    material_bin.py         # .material.bin parser/scanner
  external/
    dxil-spirv/             # DXBC/DXIL to SPIRV converter (submodule)
```

## Limitations

- Hardcoded for Minecraft Bedrock 1.21.132. Signature scan for `bgfx::init` may work across versions, but the renderer type global RVA will change.
- Shader conversion is lossy in edge cases — some complex compute shaders may not convert cleanly.
- No support for encrypted `.material.bin` files (encryption variant must be 0 / "NONE").

## Credits

- [dxil-spirv](https://github.com/HansKristian-Work/dxil-spirv) by Hans-Kristian Arntzen (Valve) — DXBC/DXIL to SPIRV conversion
- [MinHook](https://github.com/TsudaKageyu/minhook) — x64 function hooking
- [libhat](https://github.com/BasedInc/libhat) — signature scanning

## License

MIT

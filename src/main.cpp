#include <Windows.h>
#include <MinHook.h>
#include <libhat/scanner.hpp>
#include <libhat/process.hpp>
#include <cstdint>
#include <cstdio>
#include <thread>
#include <chrono>

static const char* RendererName(int type) {
    switch (type) {
    case 0: return "Direct3D 9";
    case 1: return "Direct3D 11";
    case 2: return "Direct3D 12";
    case 3: return "Direct3D 12 RTX";
    case 4: return "GNM";
    case 5: case 6: return "Noop";
    case 7: case 8: return "OpenGL 2.1";
    case 9: return "Vulkan";
    default: return "Unknown";
    }
}

// Minecraft Bedrock 1.21.132 - bgfx renderer type enum
enum class BgfxRendererType : int {
    Direct3D9    = 0,
    Direct3D11   = 1,
    Direct3D12   = 2,
    Direct3D12RT = 3,
    GNM          = 4,
    Noop0        = 5,
    Noop1        = 6,
    OpenGL0      = 7,
    OpenGL1      = 8,
    Vulkan       = 9,
};

// bgfx::init signature (address 0x146ADD160, RVA 0x6ADD160)
constexpr auto BGFX_INIT_SIG = hat::compile_signature<"40 53 56 57 48 83 EC ? 0F B6 F2">();

// RVA of dword_14970B170 - the global bgfx renderer type used by RenderDragon
// to decide which shader platform to select (Direct3D_SM50 vs GLSL etc.)
// VA 0x14970B170 - imagebase 0x140000000 = RVA 0x970B170
constexpr uintptr_t RENDERER_TYPE_GLOBAL_RVA = 0x970B170;

using BgfxInit_t = uint64_t(__fastcall*)(void* initStruct, uint8_t async);
BgfxInit_t OriginalBgfxInit = nullptr;

// Pointer to the renderer type global (resolved at runtime)
static volatile int* g_rendererTypePtr = nullptr;

uint64_t __fastcall HookedBgfxInit(void* initStruct, uint8_t async) {
    auto* rendererType = reinterpret_cast<int*>(initStruct);
    int oldType = *rendererType;
    *rendererType = static_cast<int>(BgfxRendererType::Vulkan);

    printf("[FuckDX] Forcing Vulkan renderer (was %d: %s)\n", oldType, RendererName(oldType));

    uint64_t result = OriginalBgfxInit(initStruct, async);

    // After bgfx::init completes, _initFinalize sets the renderer type global
    // to 9 (Vulkan). The RenderDragon material system uses this to select
    // shader platform variants. Since our material.bin files have Direct3D_SM50
    // shaders (with SPIRV bytecode inside), we need the material system to
    // select Direct3D entries. Patch the global back to D3D12.
    if (g_rendererTypePtr) {
        int currentType = *g_rendererTypePtr;
        printf("[FuckDX] Renderer type global = %d (%s)\n", currentType, RendererName(currentType));

        // Only patch if it was actually set to Vulkan
        if (currentType == static_cast<int>(BgfxRendererType::Vulkan)) {
            *g_rendererTypePtr = static_cast<int>(BgfxRendererType::Direct3D12);
            printf("[FuckDX] Patched renderer type global to D3D12 (%d) for shader selection\n",
                   *g_rendererTypePtr);
        } else {
            printf("[FuckDX] Renderer type global not Vulkan (%d), async init? Starting monitor thread\n",
                   currentType);

            // bgfx::init might be async - spawn a thread to wait and patch
            std::thread([]() {
                for (int i = 0; i < 100; i++) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                    int val = *g_rendererTypePtr;
                    if (val == static_cast<int>(BgfxRendererType::Vulkan)) {
                        *g_rendererTypePtr = static_cast<int>(BgfxRendererType::Direct3D12);
                        printf("[FuckDX] [thread] Patched renderer type to D3D12 after %dms\n",
                               (i + 1) * 50);
                        return;
                    }
                }
                printf("[FuckDX] [thread] Timed out waiting for Vulkan renderer type\n");
            }).detach();
        }
    }

    return result;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason != DLL_PROCESS_ATTACH)
        return TRUE;

    DisableThreadLibraryCalls(hModule);

    AllocConsole();
    freopen("CONOUT$", "w", stdout);
    SetConsoleTitleA("FuckDX");
    printf("[FuckDX] Loaded into process\n");

    auto mc = hat::process::get_module("Minecraft.Windows.exe");
    if (!mc) {
        printf("[FuckDX] Failed to find Minecraft module\n");
        return TRUE;
    }

    // Resolve the renderer type global address
    auto moduleBase = reinterpret_cast<uintptr_t>(mc->address());
    g_rendererTypePtr = reinterpret_cast<volatile int*>(moduleBase + RENDERER_TYPE_GLOBAL_RVA);
    printf("[FuckDX] Module base: %p\n", (void*)moduleBase);
    printf("[FuckDX] Renderer type global at: %p\n", (void*)g_rendererTypePtr);

    // Find and hook bgfx::init
    auto result = hat::find_pattern(BGFX_INIT_SIG, ".text", *mc);
    if (!result.has_result()) {
        printf("[FuckDX] Signature scan failed - bgfx::init not found\n");
        return TRUE;
    }

    auto target = const_cast<void*>(static_cast<const void*>(result.get()));
    printf("[FuckDX] Found bgfx::init at %p\n", target);

    if (MH_Initialize() != MH_OK) {
        printf("[FuckDX] MH_Initialize failed\n");
        return TRUE;
    }

    if (MH_CreateHook(target, &HookedBgfxInit, reinterpret_cast<void**>(&OriginalBgfxInit)) != MH_OK) {
        printf("[FuckDX] MH_CreateHook failed\n");
        return TRUE;
    }

    if (MH_EnableHook(target) != MH_OK) {
        printf("[FuckDX] MH_EnableHook failed\n");
        return TRUE;
    }

    printf("[FuckDX] Hook installed - Vulkan renderer will be forced\n");
    printf("[FuckDX] NOTE: Material .bin files must be pre-converted with SPIRV shaders!\n");
    printf("[FuckDX]       Without converted materials, the game WILL crash (TDR).\n");
    return TRUE;
}

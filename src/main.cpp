#include <Windows.h>
#include <MinHook.h>
#include <libhat/scanner.hpp>
#include <libhat/process.hpp>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <filesystem>

// ============================================================
// FuckDX - Force Vulkan renderer in Minecraft Bedrock (GDK)
//
// Hooks bgfx::init to force the Vulkan backend (type 10)
// instead of D3D12 RTX (type 4). Material files are redirected
// to pre-converted SPIRV variants via a CreateFileW hook.
//
// Three patches are required for correct rendering:
//   1. Init struct type -> 10 (Vulkan)
//   2. Renderer global  -> 4  (D3D12 RTX) after init, so the
//      game selects D3D12 shader variants from materials
//   3. vkCmdSetViewport -> negative height Y-flip to compensate
//      for Vulkan's inverted clip-space Y vs D3D
// ============================================================

// --- Globals ---
static FILE* g_logFile = nullptr;
static std::filesystem::path g_convertedDir;
static volatile int* g_rendererTypePtr = nullptr;
static uint8_t* g_backendTable = nullptr;

static void Log(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    if (g_logFile) {
        vfprintf(g_logFile, fmt, args);
        fflush(g_logFile);
    }
    va_end(args);
}

// --- Signature patterns ---
// Scanned at startup to locate bgfx internals. If a new game version
// changes the compiled code, only these patterns need updating.
constexpr auto BGFX_INIT_SIG       = hat::compile_signature<"40 53 56 57 48 83 EC ? 0F B6 F2">();
constexpr auto BACKEND_TABLE_SIG   = hat::compile_signature<"BE 84 01 00 00 48 8B D9 48 8D 2D">();
constexpr auto RENDERER_GLOBAL_SIG = hat::compile_signature<"48 8B 01 48 8B 40 08 FF 15 ? ? ? ? 89 05">();

// --- Hook types ---
using BgfxInit_t = uint64_t(__fastcall*)(void* initStruct, uint8_t async);
static BgfxInit_t OriginalBgfxInit = nullptr;

using CreateFileW_t = HANDLE(WINAPI*)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
static CreateFileW_t OriginalCreateFileW = nullptr;

// --- vkCmdSetViewport Y-flip ---
// VkViewport: float x, y, width, height, minDepth, maxDepth (24 bytes)
using PFN_vkCmdSetViewport = void(__stdcall*)(
    void* commandBuffer, uint32_t firstViewport, uint32_t viewportCount, const float* pViewports);
static PFN_vkCmdSetViewport g_origVkCmdSetViewport = nullptr;

void __stdcall HookedVkCmdSetViewport(
    void* commandBuffer, uint32_t firstViewport, uint32_t viewportCount, const float* pViewports)
{
    // Flip Y: y' = y + height, height' = -height
    // Compensates for Vulkan's inverted clip-space Y vs D3D.
    // Requires VK_KHR_maintenance1 (core in Vulkan 1.1+).
    float flipped[6 * 16];
    uint32_t count = viewportCount < 16 ? viewportCount : 16;
    memcpy(flipped, pViewports, count * 24);
    for (uint32_t i = 0; i < count; i++) {
        float* vp = flipped + i * 6;
        vp[1] = vp[1] + vp[3];
        vp[3] = -vp[3];
    }
    g_origVkCmdSetViewport(commandBuffer, firstViewport, count, flipped);
}

// --- vkGetDeviceProcAddr hook ---
// Intercepts Vulkan function pointer resolution to hook vkCmdSetViewport
// without needing a hardcoded offset. Works across game versions.
using PFN_vkGetDeviceProcAddr = void*(__stdcall*)(void* device, const char* pName);
static PFN_vkGetDeviceProcAddr OriginalVkGetDeviceProcAddr = nullptr;

void* __stdcall HookedVkGetDeviceProcAddr(void* device, const char* pName) {
    void* result = OriginalVkGetDeviceProcAddr(device, pName);
    if (result && pName && strcmp(pName, "vkCmdSetViewport") == 0) {
        g_origVkCmdSetViewport = reinterpret_cast<PFN_vkCmdSetViewport>(result);
        Log("[FuckDX] Intercepted vkCmdSetViewport via vkGetDeviceProcAddr\n");
        return reinterpret_cast<void*>(HookedVkCmdSetViewport);
    }
    return result;
}

// --- DisableBackend: zero the "supported" byte for a backend entry ---
static void DisableBackend(int index) {
    auto factoryBase = g_backendTable - 0x20;
    auto supByte = factoryBase + index * 0x28 + 0x18;
    DWORD oldProt;
    if (VirtualProtect(supByte, 1, PAGE_READWRITE, &oldProt)) {
        supByte[0] = 0;
        VirtualProtect(supByte, 1, oldProt, &oldProt);
    }
}

// --- Material redirect counters ---
static int g_materialRedirects = 0;

// --- CreateFileW hook: redirect .material.bin to converted SPIRV ---
HANDLE WINAPI HookedCreateFileW(
    LPCWSTR lpFileName, DWORD dwDesiredAccess, DWORD dwShareMode,
    LPSECURITY_ATTRIBUTES lpSA, DWORD dwCreation, DWORD dwFlags, HANDLE hTemplate)
{
    if (lpFileName) {
        std::wstring_view path(lpFileName);
        if (path.size() > 13 && path.substr(path.size() - 13) == L".material.bin") {
            auto slash = path.rfind(L'\\');
            if (slash == std::wstring_view::npos) slash = path.rfind(L'/');
            std::wstring filename(slash != std::wstring_view::npos
                ? path.substr(slash + 1) : path);

            auto converted = g_convertedDir / filename;
            if (GetFileAttributesW(converted.c_str()) != INVALID_FILE_ATTRIBUTES) {
                g_materialRedirects++;
                return OriginalCreateFileW(converted.c_str(), dwDesiredAccess, dwShareMode,
                    lpSA, dwCreation, dwFlags, hTemplate);
            }
        }
    }
    return OriginalCreateFileW(lpFileName, dwDesiredAccess, dwShareMode, lpSA, dwCreation, dwFlags, hTemplate);
}

// --- bgfx::init hook ---
static int g_initCount = 0;

uint64_t __fastcall HookedBgfxInit(void* initStruct, uint8_t async) {
    g_initCount++;
    auto* rendererType = reinterpret_cast<int*>(initStruct);

    // Force Vulkan (backend table entry [10])
    *rendererType = 10;

    // Disable D3D12 backends to prevent fallback
    if (g_backendTable) {
        DisableBackend(3);  // Direct3D 12
        DisableBackend(4);  // D3D12 RTX
    }

    uint64_t result = OriginalBgfxInit(initStruct, async);

    if (result != 0) {
        Log("[FuckDX] bgfx::init FAILED (returned %llu)\n", result);
        return result;
    }
    Log("[FuckDX] bgfx::init #%d OK (Vulkan)\n", g_initCount);

    // Patch renderer global back to D3D12 RTX (4) for shader variant selection.
    // The game uses this to pick which shader platform to load from materials.
    // Our converted materials have SPIRV in the D3D12 slots.
    if (g_rendererTypePtr) {
        *g_rendererTypePtr = 4;
    }

    Log("[FuckDX] Materials redirected so far: %d\n", g_materialRedirects);
    return result;
}

// --- DllMain ---
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason != DLL_PROCESS_ATTACH)
        return TRUE;

    DisableThreadLibraryCalls(hModule);

    // Set up paths
    wchar_t dllPath[MAX_PATH];
    GetModuleFileNameW(hModule, dllPath, MAX_PATH);
    auto dllDir = std::filesystem::path(dllPath).parent_path();

    wchar_t exePath[MAX_PATH];
    GetModuleFileNameW(nullptr, exePath, MAX_PATH);
    auto gameDir = std::filesystem::path(exePath).parent_path();

    g_convertedDir = gameDir / L"data" / L"renderer" / L"converted_materials";
    _wfopen_s(&g_logFile, (dllDir / L"fuckdx.log").c_str(), L"w");

    Log("[FuckDX] FuckDX loaded\n");

    // Find game module
    auto mc = hat::process::get_module("Minecraft.Windows.exe");
    if (!mc) { Log("[FuckDX] Module not found\n"); return TRUE; }

    // Resolve signatures
    auto initResult = hat::find_pattern(BGFX_INIT_SIG, ".text", *mc);
    if (!initResult.has_result()) { Log("[FuckDX] bgfx::init sig not found\n"); return TRUE; }
    Log("[FuckDX] bgfx::init found at %p\n", initResult.get());

    auto tableResult = hat::find_pattern(BACKEND_TABLE_SIG, ".text", *mc);
    if (tableResult.has_result()) {
        auto leaAddr = reinterpret_cast<const uint8_t*>(tableResult.get()) + 8;
        int32_t disp = *reinterpret_cast<const int32_t*>(leaAddr + 3);
        g_backendTable = const_cast<uint8_t*>(leaAddr + 7 + disp);
        Log("[FuckDX] Backend table at %p\n", g_backendTable);
    } else {
        Log("[FuckDX] Backend table sig not found (non-fatal)\n");
    }

    auto globalResult = hat::find_pattern(RENDERER_GLOBAL_SIG, ".text", *mc);
    if (globalResult.has_result()) {
        auto movAddr = reinterpret_cast<const uint8_t*>(globalResult.get()) + 13;
        int32_t disp = *reinterpret_cast<const int32_t*>(movAddr + 2);
        g_rendererTypePtr = reinterpret_cast<volatile int*>(
            const_cast<uint8_t*>(movAddr) + 6 + disp);
        Log("[FuckDX] Renderer global at %p\n", (void*)g_rendererTypePtr);
    } else {
        Log("[FuckDX] Renderer global sig not found (non-fatal)\n");
    }

    // Install hooks
    if (MH_Initialize() != MH_OK) { Log("[FuckDX] MinHook init failed\n"); return TRUE; }

    auto target = const_cast<void*>(static_cast<const void*>(initResult.get()));
    MH_CreateHook(target, &HookedBgfxInit, reinterpret_cast<void**>(&OriginalBgfxInit));

    HMODULE hKB = GetModuleHandleA("KernelBase.dll");
    if (hKB) {
        auto pCFW = GetProcAddress(hKB, "CreateFileW");
        if (pCFW)
            MH_CreateHook(pCFW, &HookedCreateFileW, reinterpret_cast<void**>(&OriginalCreateFileW));
    }

    // Hook vkGetDeviceProcAddr for version-independent viewport Y-flip.
    // When bgfx resolves vkCmdSetViewport, we intercept and return our hook.
    HMODULE hVulkan = LoadLibraryA("vulkan-1.dll");
    if (hVulkan) {
        auto pGDPA = GetProcAddress(hVulkan, "vkGetDeviceProcAddr");
        if (pGDPA)
            MH_CreateHook(pGDPA, &HookedVkGetDeviceProcAddr,
                reinterpret_cast<void**>(&OriginalVkGetDeviceProcAddr));
        Log("[FuckDX] Vulkan loader hooked (vkGetDeviceProcAddr)\n");
    } else {
        Log("[FuckDX] vulkan-1.dll not found (viewport Y-flip disabled)\n");
    }

    if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) {
        Log("[FuckDX] Hook enable failed\n"); return TRUE;
    }

    Log("[FuckDX] Hooks installed, waiting for bgfx::init\n");
    return TRUE;
}

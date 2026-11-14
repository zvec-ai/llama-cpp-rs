param([switch]$DriverOnly)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

# The hosted GPU image is a Data Science VM, not the standard Windows CI image.
# Provision only the ephemeral Actions host; never reboot it.
if ($env:GITHUB_ACTIONS -ne "true" -or $env:OS -ne "Windows_NT") {
    throw "This helper is only for ephemeral Windows GitHub Actions runners"
}

# Fixed Data Center driver for T4 / Server 2022 / CUDA 13.1. Toolkit and
# runtime-only installers do not update the GPU image's old NVIDIA driver.
# https://www.microsoft.com/en-us/download/details.aspx?id=108602
$driverVersion = [version]"591.59"
$driverUrl = "https://download.microsoft.com/download/8920fc6c-3546-458b-99d9-647c4e8621a4/591.59-data-center-tesla-desktop-win10-win11-64bit-dch-international.exe"

function Get-GpuDriverVersions {
    # Resolve again after installation: do not keep using an old NVSMI path.
    $smi = @(
        (Join-Path $env:SystemRoot "System32/nvidia-smi.exe"),
        (Join-Path $env:ProgramFiles "NVIDIA Corporation/NVSMI/nvidia-smi.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $smi) { $smi = (Get-Command nvidia-smi.exe -ErrorAction Stop).Source }
    $output = & $smi --query-gpu=driver_version --format=csv,noheader
    if ($LASTEXITCODE -ne 0) { throw "nvidia-smi driver query failed (exit code $LASTEXITCODE)" }
    $versions = @($output | ForEach-Object { [version]($_.Trim()) })
    if ($versions.Count -eq 0) { throw "nvidia-smi did not report any GPU drivers" }
    Write-Host "Active NVIDIA driver versions: $($versions -join ', ')"
    return $versions
}

function Assert-DriverInstallExitCode([int]$Code) {
    # NVIDIA uses 1 for reboot-required; also reject Windows Installer's
    # reboot-required / reboot-initiated results. Never restart this runner.
    if ($Code -in @(1, 3010, 1641)) {
        throw "NVIDIA driver installation requires a reboot (exit code $Code). This job will not reboot the runner; CUDA verification cannot continue."
    }
    if ($Code -ne 0) { throw "NVIDIA driver installation failed (exit code $Code)" }
}

$versions = @(Get-GpuDriverVersions)
if (@($versions | Where-Object { $_ -lt $driverVersion }).Count -ne 0) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Updating the NVIDIA driver requires an elevated ephemeral GPU runner"
    }
    $driverInstaller = Join-Path $env:RUNNER_TEMP "zg-nvidia-$driverVersion.exe"
    & curl.exe --fail --location --proto '=https' --retry 3 --max-time 600 `
        $driverUrl --output $driverInstaller
    if ($LASTEXITCODE -ne 0) { throw "Downloading NVIDIA driver $driverVersion failed" }
    # No official checksum is published for this fixed download. Authenticate
    # the signed installer before executing it and record its hash for audit.
    $signature = Get-AuthenticodeSignature -FilePath $driverInstaller
    if ($signature.Status -ne "Valid" -or -not $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne "NVIDIA Corporation") {
        throw "NVIDIA driver installer signature is not trusted"
    }
    Write-Host "NVIDIA installer SHA256: $((Get-FileHash -Algorithm SHA256 -LiteralPath $driverInstaller).Hash)"
    # NVIDIA's own Windows CI invokes the self-extracting installer with these
    # switches. /noreboot is mandatory, even on this disposable runner.
    # https://github.com/NVIDIA/cuda-python/blob/main/ci/tools/install_gpu_driver.ps1
    $result = Start-Process -FilePath $driverInstaller `
        -ArgumentList @('/s', '/noeula', '/noreboot') -Wait -PassThru
    Assert-DriverInstallExitCode $result.ExitCode
    $versions = @(Get-GpuDriverVersions)
    if (@($versions | Where-Object { $_ -lt $driverVersion }).Count -ne 0) {
        throw "NVIDIA driver $driverVersion is not active after installation; a reboot may be required. This job will not reboot the runner."
    }
}

# A successful nvidia-smi query is not proof that CUDA can initialize. Load
# the system driver (not a toolkit DLL), and verify its API and real devices.
@'
import ctypes
import os

cuda = ctypes.WinDLL(os.path.join(os.environ["SystemRoot"], "System32", "nvcuda.dll"))
cuda.cuGetErrorString.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
cuda.cuGetErrorString.restype = ctypes.c_int
cuda.cuInit.argtypes = [ctypes.c_uint]
cuda.cuInit.restype = ctypes.c_int
cuda.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]
cuda.cuDriverGetVersion.restype = ctypes.c_int
cuda.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
cuda.cuDeviceGetCount.restype = ctypes.c_int

def check(code, operation):
    if code:
        message = ctypes.c_char_p()
        cuda.cuGetErrorString(code, ctypes.byref(message))
        detail = message.value.decode("utf-8", errors="replace") if message.value else "unknown error"
        raise RuntimeError(f"{operation} failed: CUDA error {code}: {detail}. No automatic reboot will be attempted.")

version = ctypes.c_int()
check(cuda.cuDriverGetVersion(ctypes.byref(version)), "cuDriverGetVersion")
if version.value < 13010:
    raise RuntimeError(f"Active CUDA driver API {version.value} does not support CUDA 13.1; no automatic reboot will be attempted")
check(cuda.cuInit(0), "cuInit")
count = ctypes.c_int()
check(cuda.cuDeviceGetCount(ctypes.byref(count)), "cuDeviceGetCount")
if count.value < 1:
    raise RuntimeError("CUDA driver initialized but no GPU devices are available")
print(f"Verified NVIDIA driver: CUDA API {version.value}, {count.value} GPU device(s)", flush=True)
'@ | python -
if ($LASTEXITCODE -ne 0) { throw "Windows CUDA driver verification failed; this job will not reboot the runner" }
if ($DriverOnly) { return }

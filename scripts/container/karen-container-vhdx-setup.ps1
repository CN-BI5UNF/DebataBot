<#
.SYNOPSIS
  KarenBot C-mode container storage on Windows: fixed-size VHDX + NTFS drive.

.DESCRIPTION
  One-time elevated setup. Creates a fixed VHDX with diskpart, attaches it,
  formats NTFS, assigns a drive letter, creates <drive>:\workspace, and writes
  the .karen-container-storage.json marker checked by ContainerStorage.

  This script has NOT been validated on a real Windows machine in this worktree.
  Run it only after reviewing it, and keep Docker Desktop in Linux-container
  mode. It does not resize or delete existing VHDX data.

.PARAMETER VhdxPath
  e.g. C:\Users\me\AppData\Local\KarenBot\instances\default\data\container\workspace.vhdx

.PARAMETER MountPoint
  Drive-based workspace path, e.g. K:\workspace. The drive letter is derived
  from this path. A plain folder path is rejected in v1.

.PARAMETER SizeMB
  Fixed VHDX size in MiB. Must match features.container.workspace_size_mb.
#>
param(
    [Parameter(Mandatory = $true)][string]$VhdxPath,
    [Parameter(Mandatory = $true)][string]$MountPoint,
    [int]$SizeMB = 2048
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "需要管理员 PowerShell。请从 WebUI 首次启用弹窗确认 UAC，或以管理员身份重新运行本脚本。"
}
if ($SizeMB -lt 64) { throw "SizeMB 必须 >= 64" }
if ($MountPoint -notmatch '^([A-Za-z]):\\') {
    throw "v1 仅支持盘符路径（例如 K:\workspace）；请把 image_fixed_mountpoint 配置为盘符下的 workspace。"
}
$Letter = $Matches[1].ToUpperInvariant()
$VhdxPath = [System.IO.Path]::GetFullPath($VhdxPath)
$MountPoint = [System.IO.Path]::GetFullPath($MountPoint)
$VhdxDir = [System.IO.Path]::GetDirectoryName($VhdxPath)
New-Item -ItemType Directory -Force -Path $VhdxDir | Out-Null
New-Item -ItemType Directory -Force -Path $MountPoint | Out-Null

if (-not (Test-Path -LiteralPath $VhdxPath)) {
    $diskpartScript = Join-Path $env:TEMP ("karen-vhdx-" + [guid]::NewGuid().ToString("N") + ".txt")
    @"
create vdisk file="$VhdxPath" maximum=$SizeMB type=fixed
select vdisk file="$VhdxPath"
attach vdisk
create partition primary
format fs=ntfs quick label=karen-container
assign letter=$Letter
"@ | Set-Content -LiteralPath $diskpartScript -Encoding ASCII
    try {
        $output = & diskpart.exe /s $diskpartScript 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "diskpart 失败: $output"
        }
    } finally {
        Remove-Item -LiteralPath $diskpartScript -Force -ErrorAction SilentlyContinue
    }
} else {
    $diskpartScript = Join-Path $env:TEMP ("karen-vhdx-" + [guid]::NewGuid().ToString("N") + ".txt")
    @"
select vdisk file="$VhdxPath"
attach vdisk
"@ | Set-Content -LiteralPath $diskpartScript -Encoding ASCII
    try {
        $output = & diskpart.exe /s $diskpartScript 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "挂载已有 VHDX 失败: $output"
        }
    } finally {
        Remove-Item -LiteralPath $diskpartScript -Force -ErrorAction SilentlyContinue
    }
}

$workspace = $MountPoint
New-Item -ItemType Directory -Force -Path $workspace | Out-Null
$marker = Join-Path $VhdxDir ".karen-container-storage.json"
@{
    mode = "image_fixed"
    size_mb = $SizeMB
    created_at = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $marker -Encoding UTF8

Write-Host "C 存储已就绪: $workspace"
Write-Host "请将 features.container.image_fixed_mountpoint 配置为: $workspace"
Write-Host "Docker Desktop 需要保持 Linux containers 模式；重启后 VHDX 可能需要在磁盘管理中重新附加。"
Write-Host "本脚本未在真实 Windows 机器上验收；请先做最小写入测试，不要做压力测试。"

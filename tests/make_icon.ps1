# Generate multi-size .ico from a source image (PNG/JPG).
# Usage: powershell -ExecutionPolicy Bypass -File tests\make_icon.ps1 -Src data\brand\app_icon.jpg -Dst data\brand\app_icon.ico
# All-ASCII script to avoid encoding issues on Chinese Windows (GBK codepage).
param(
    [string]$Src = "data\brand\app_icon.jpg",
    [string]$Dst = "data\brand\app_icon.ico"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$srcPath = Join-Path (Split-Path $PSScriptRoot -Parent) $Src
$dstPath = Join-Path (Split-Path $PSScriptRoot -Parent) $Dst
if (-not (Test-Path $srcPath)) { throw "source image not found: $srcPath" }

$sizes = @(256, 128, 64, 48, 32, 24, 16)
$img = [System.Drawing.Image]::FromFile($srcPath)
$pngs = @()
$offset = 6 + 16 * $sizes.Count

foreach ($s in $sizes) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($img, 0, 0, $s, $s)
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngs += , $ms.ToArray()
    $bmp.Dispose()
    $ms.Dispose()
}
$img.Dispose()

$fs = [System.IO.File]::Create($dstPath)
$bw = New-Object System.IO.BinaryWriter $fs
$bw.Write([uint16]0)                       # reserved
$bw.Write([uint16]1)                       # type: icon
$bw.Write([uint16]$sizes.Count)            # image count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]
    $len = $pngs[$i].Length
    $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s })))   # width (0 means 256)
    $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s })))   # height
    $bw.Write([byte]0)                     # color palette
    $bw.Write([byte]0)                     # reserved
    $bw.Write([uint16]1)                   # color planes
    $bw.Write([uint16]32)                  # bits per pixel
    $bw.Write([uint32]$len)                # PNG data size
    $bw.Write([uint32]$offset)             # data offset
    $offset += $len
}
for ($i = 0; $i -lt $pngs.Count; $i++) {
    $bw.Write($pngs[$i])
}
$bw.Close()
Write-Host "ICO generated: $dstPath (sizes: $($sizes -join ','))"
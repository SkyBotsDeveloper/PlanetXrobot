param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,
    [string]$OutputPath = "hrtf_8d_preview.wav",
    [double]$StartPosition = 0,
    [switch]$Realtime
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$assetDir = Join-Path $root "PLANETXROBOT\assets\hrir"
$angles = @("000", "045", "090", "135", "180", "225", "270", "315")
$inputs = @()
foreach ($angle in $angles) {
    $hrir = Join-Path $assetDir "kemar_$angle.wav"
    if (-not (Test-Path -LiteralPath $hrir)) { throw "Missing HRIR asset: $hrir" }
    $inputs += @("-i", $hrir, "-i", $hrir)
}

$split = "[16:a]aformat=channel_layouts=stereo,asplit=9[d0]"
for ($i = 0; $i -lt 8; $i++) { $split += "[s$i]" }
$graph = @(
    $split,
    "[d0]pan=stereo|FL=0.5*FL+0.5*FR|FR=0.5*FL+0.5*FR,volume=0.96[mid]"
)
$mix = "[mid]"
for ($i = 0; $i -lt 8; $i++) {
    $angle = "${i}*PI/4"
    $distance = "abs(mod(2*PI*0.03125*(t+$StartPosition)-$angle+PI,2*PI)-PI)"
    $weight = "0.34*if(lt($distance,PI/4),cos(2*$distance),0)"
    $left = $i * 2
    $right = $left + 1
    $graph += "[s$i]pan=stereo|FL=0.5*FL-0.5*FR|FR=0*FR[side$i]"
    $graph += "[side$i][$left`:a][$right`:a]headphone=map=FL|FR:hrir=stereo:type=freq:size=1024[h$i]"
    $graph += "[h$i]volume='$weight':eval=frame[hw$i]"
    $mix += "[hw$i]"
}
$graph += "${mix}amix=inputs=9:normalize=0,alimiter=limit=0.98:attack=5:release=80:latency=0[out]"

$programInput = if ($Realtime) { @("-re", "-i", $InputPath) } else { @("-i", $InputPath) }
& ffmpeg -hide_banner -loglevel error @inputs @programInput -filter_complex ($graph -join ";") `
    -map "[out]" -ar 48000 -ac 2 -c:a pcm_s16le -y $OutputPath
if ($LASTEXITCODE -ne 0) { throw "FFmpeg failed with exit code $LASTEXITCODE" }

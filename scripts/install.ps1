param(
  [string]$OpenmdIndex = 'https://pypi.tuna.tsinghua.edu.cn/simple'
)
$ErrorActionPreference = 'Stop'
$uvVersion = '0.13.9'
$uvSha256 = 'edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb'
$installRoot = Join-Path $env:LOCALAPPDATA 'Openmd'
$managerDir = Join-Path $installRoot 'manager'
$uvExe = Join-Path $managerDir 'uv.exe'
$toolDir = Join-Path $installRoot 'uv\tools'
$toolBin = Join-Path $installRoot 'bin'
$pythonDir = Join-Path $installRoot 'uv\python'
$cacheDir = Join-Path $installRoot 'uv\cache'
$temporary = Join-Path $installRoot 'download'
foreach ($dir in @($managerDir,$toolDir,$toolBin,$pythonDir,$cacheDir,$temporary)) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

if (-not (Test-Path -LiteralPath $uvExe)) {
  $asset = "uv-x86_64-pc-windows-msvc.zip"
  $urls = @(
    "https://releases.astral.sh/github/uv/releases/download/$uvVersion/$asset",
    "https://github.com/astral-sh/uv/releases/download/$uvVersion/$asset"
  )
  $archive = Join-Path $temporary $asset
  $downloaded = $false
  foreach ($url in $urls) {
    try {
      Invoke-WebRequest -Uri $url -OutFile $archive -TimeoutSec 60
      if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvSha256) {
        throw 'uv_release_checksum_mismatch'
      }
      $downloaded = $true
      break
    } catch {
      Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not $downloaded) { throw 'uv_download_or_checksum_failed' }
  $expanded = Join-Path $temporary 'expanded'
  Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
  $candidate = Get-ChildItem -LiteralPath $expanded -Filter 'uv.exe' -File -Recurse | Select-Object -First 1
  if (-not $candidate) { throw 'uv_executable_missing_from_archive' }
  Copy-Item -LiteralPath $candidate.FullName -Destination $uvExe -Force
}
if (-not (& $uvExe --version).StartsWith("uv $uvVersion")) { throw 'uv_version_mismatch' }

$env:UV_TOOL_DIR = $toolDir
$env:UV_TOOL_BIN_DIR = $toolBin
$env:UV_PYTHON_INSTALL_DIR = $pythonDir
$env:UV_CACHE_DIR = $cacheDir
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:OPENMD_NPM_REGISTRY = 'https://registry.npmmirror.com'
$env:HF_ENDPOINT = 'https://hf-mirror.com'
$env:OPENMD_DATA_DIR = if ($env:OPENMD_DATA_DIR) { $env:OPENMD_DATA_DIR } else { Join-Path $env:USERPROFILE '.openmd' }
$env:PATH = "$toolBin;$env:PATH"
$openmd = Join-Path $toolBin 'openmd.exe'
$spec = 'openmd-cli[all]'
$successfulIndex = $OpenmdIndex
$verb = if (Test-Path -LiteralPath $openmd) { 'upgrade' } else { 'install' }
& $uvExe 'tool' $verb $spec '--python' '3.12' '--python-preference' 'only-managed' '--index-url' $OpenmdIndex
if ($LASTEXITCODE -ne 0) {
  $fallback = 'https://mirrors.aliyun.com/pypi/simple'
  & $uvExe 'tool' $verb $spec '--python' '3.12' '--python-preference' 'only-managed' '--index-url' $fallback
  $successfulIndex = $fallback
}
if ($LASTEXITCODE -ne 0) { throw 'openmd_install_failed_on_both_pypi_indexes' }
if (-not (Test-Path -LiteralPath $openmd)) { throw 'openmd_launcher_missing' }

& $openmd browser-setup
if ($LASTEXITCODE -ne 0) { throw 'browser_setup_failed' }
$doctor = & $openmd doctor --profile all --json
$doctorCode = $LASTEXITCODE
$cardPath = Join-Path $env:OPENMD_DATA_DIR 'runs\install-capability-card.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cardPath) | Out-Null
$doctor | Set-Content -LiteralPath $cardPath -Encoding utf8

$recordPath = Join-Path $env:OPENMD_DATA_DIR 'install.json'
New-Item -ItemType Directory -Force -Path $env:OPENMD_DATA_DIR | Out-Null
$record = [ordered]@{schema='openmd.install/v1';manager='uv';manager_exe=$uvExe;tool_dir=$toolDir;tool_bin=$toolBin;python_dir=$pythonDir;install_spec=$spec;install_index=$successfulIndex;managed_opencli_npm_registry=$env:OPENMD_NPM_REGISTRY;hf_endpoint=$env:HF_ENDPOINT}
$recordTemp = "$recordPath.tmp"
$record | ConvertTo-Json | Set-Content -LiteralPath $recordTemp -Encoding utf8
Move-Item -LiteralPath $recordTemp -Destination $recordPath -Force

Write-Output "Openmd 已安装。命令：$openmd；能力卡：$cardPath（doctor exit $doctorCode；缺少可选模型时按卡片显示的 next_action 处理）。"
Write-Output 'Chrome 中请手动打开 chrome://extensions，启用“开发者模式”，选择“加载已解压的扩展程序”，再选 Openmd 显示的扩展目录。'
Write-Output 'npm 镜像已为托管 OpenCLI 设置；HF 镜像已为后续模型下载设置。安装期间不会下载语音模型。'

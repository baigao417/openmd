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
  # Windows PowerShell 5.1: the progress bar slows Invoke-WebRequest by orders
  # of magnitude, and older .NET defaults may not offer TLS 1.2.
  $ProgressPreference = 'SilentlyContinue'
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
  $asset = "uv-x86_64-pc-windows-msvc.zip"
  # The same uv build is also published on PyPI as a wheel (a zip with uv.exe
  # inside); Chinese PyPI mirrors reach it when GitHub does not. Each source
  # has its own pinned SHA-256 (wheel digest cross-checked with pypi.org).
  $uvWheel = "uv-$uvVersion-py3-none-win_amd64.whl"
  $uvWheelSha256 = '6a44b2ed8111d2f60d2b92627c5dccda3f7ed7570b18ce115e569cd68c28246b'
  $uvWheelPath = 'packages/dd/ef/ceeabf3631117f7354787d9fb1ffe943899e59ed4b887a696ed547e47e76'
  $sources = @(
    @{ url = "https://releases.astral.sh/github/uv/releases/download/$uvVersion/$asset"; sha = $uvSha256 },
    @{ url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$asset"; sha = $uvSha256 },
    @{ url = "https://pypi.tuna.tsinghua.edu.cn/$uvWheelPath/$uvWheel"; sha = $uvWheelSha256 },
    @{ url = "https://mirrors.aliyun.com/pypi/$uvWheelPath/$uvWheel"; sha = $uvWheelSha256 },
    @{ url = "https://files.pythonhosted.org/$uvWheelPath/$uvWheel"; sha = $uvWheelSha256 }
  )
  $archive = Join-Path $temporary 'uv-download.zip'
  $downloaded = $false
  $failures = @()
  foreach ($source in $sources) {
    try {
      Invoke-WebRequest -Uri $source.url -OutFile $archive -TimeoutSec 120 -UseBasicParsing
      if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $source.sha) {
        throw 'checksum_mismatch'
      }
      $downloaded = $true
      break
    } catch {
      $failures += "$(([Uri]$source.url).Host): $($_.Exception.Message)"
      Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not $downloaded) {
    $failures | ForEach-Object { Write-Warning $_ }
    throw 'uv_download_or_checksum_failed: check the network or proxy, then rerun the install command'
  }
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

# A new terminal must find `openmd`: add the launcher directory to the user
# PATH in the registry (raw value and type kept, so %VAR% entries survive) and
# tell Explorer the environment changed.
$pathAdded = $false
$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$rawPath = $envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$pathKind = if ($rawPath) { $envKey.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
$pathEntries = @($rawPath -split ';' | Where-Object { $_ })
if (-not ($pathEntries | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $toolBin.TrimEnd('\') })) {
  $envKey.SetValue('Path', (@($pathEntries + $toolBin) -join ';'), $pathKind)
  $pathAdded = $true
}
$envKey.Close()
if ($pathAdded) {
  Add-Type -Namespace OpenmdInstall -Name Native -MemberDefinition '[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern System.IntPtr SendMessageTimeout(System.IntPtr h, uint m, System.UIntPtr w, string l, uint f, uint t, out System.UIntPtr r);'
  $broadcastResult = [System.UIntPtr]::Zero
  [void][OpenmdInstall.Native]::SendMessageTimeout([System.IntPtr]0xffff, 0x1A, [System.UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$broadcastResult)
}

& $openmd browser-setup
if ($LASTEXITCODE -ne 0) { throw 'browser_setup_failed' }
$doctor = & $openmd doctor --profile all --json
$doctorCode = $LASTEXITCODE
$cardPath = Join-Path $env:OPENMD_DATA_DIR 'runs\install-capability-card.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cardPath) | Out-Null
$doctor | Set-Content -LiteralPath $cardPath -Encoding utf8

$recordPath = Join-Path $env:OPENMD_DATA_DIR 'install.json'
New-Item -ItemType Directory -Force -Path $env:OPENMD_DATA_DIR | Out-Null
$record = [ordered]@{schema='openmd.install/v1';manager='uv';manager_exe=$uvExe;tool_dir=$toolDir;tool_bin=$toolBin;python_dir=$pythonDir;cache_dir=$cacheDir;install_root=$installRoot;path_entry_added=$pathAdded;install_spec=$spec;install_index=$successfulIndex;managed_opencli_npm_registry=$env:OPENMD_NPM_REGISTRY;hf_endpoint=$env:HF_ENDPOINT}
$recordTemp = "$recordPath.tmp"
$record | ConvertTo-Json | Set-Content -LiteralPath $recordTemp -Encoding utf8
Move-Item -LiteralPath $recordTemp -Destination $recordPath -Force

Write-Output "Openmd 已安装。新开一个终端即可直接输入 openmd（当前窗口也已可用）。命令：$openmd；能力卡：$cardPath（doctor exit $doctorCode；缺少可选模型时按卡片显示的 next_action 处理）。"
Write-Output 'Chrome 中请手动打开 chrome://extensions，启用“开发者模式”，选择“加载已解压的扩展程序”，再选 Openmd 显示的扩展目录。'
Write-Output 'npm 镜像已为托管 OpenCLI 设置；HF 镜像已为后续模型下载设置。安装期间不会下载语音模型。'

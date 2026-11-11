# 语音搭子「声音」扩展的安装：建 .venv（Python 3.12）、装依赖、从 ModelScope 下载语音模型（约 2.4 GB）。
# 在这个文件夹里运行：powershell -ExecutionPolicy Bypass -File setup.ps1
#   -SkipModel   不下载模型（已经有 models\Qwen3-TTS-12Hz-0.6B-CustomVoice 时会自动跳过）
#   -Python <路径> 指定 Python 3.12 的 python.exe
param([switch]$SkipModel, [string]$Python = "")

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$model = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
$modelDir = Join-Path $root "models\Qwen3-TTS-12Hz-0.6B-CustomVoice"
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

Step "检查显卡"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} else {
    Write-Warning "没找到 nvidia-smi：语音合成需要 NVIDIA 显卡和支持 CUDA 12.8 的驱动。没有显卡也能装完，但模型载不进来。"
}

Step "准备 Python 3.12 虚拟环境"
if (-not (Test-Path $py)) {
    if ($Python -eq "") {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            try {
                $ErrorActionPreference = "Continue"
                $found = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
                if ($LASTEXITCODE -eq 0 -and $found) { $Python = "$found".Trim() }
            } catch { } finally { $ErrorActionPreference = "Stop" }
        }
        if ($Python -eq "" -and (Get-Command python -ErrorAction SilentlyContinue)) {
            $v = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])")
            if ($v -eq "3.12") { $Python = (Get-Command python).Source }
        }
    }
    if ($Python -eq "") { throw "没找到 Python 3.12。请从 python.org 安装 3.12，或用 -Python 指定它的 python.exe。" }
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "建虚拟环境失败" }
} else {
    Write-Host "已有 .venv，沿用"
}

Step "安装依赖（PyTorch 约 2.5 GB，第一次要等一会儿）"
& $py -m pip install --upgrade pip
& $py -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖没装上：看上面 pip 的报错" }
& $py -c "import torch; print('CUDA 可用：', torch.cuda.is_available())"

Step "下载语音模型"
if ($SkipModel) {
    Write-Host "跳过（-SkipModel）"
} elseif (Test-Path (Join-Path $modelDir "model.safetensors")) {
    Write-Host "已有 $modelDir，跳过"
} else {
    & $py -m pip install modelscope
    & $py -c "import sys; from modelscope import snapshot_download; snapshot_download(sys.argv[1], local_dir=sys.argv[2])" $model $modelDir
    if ($LASTEXITCODE -ne 0) { throw "模型没下载完：再运行一次 setup.ps1 会接着下" }
}

Step "完成"
Write-Host "下一步：在无限工坊 App 的「扩展」页点「添加」，选这个文件夹，看过摘要后批准。"
Write-Host "然后在游戏里输入 /wk，在「语音搭子」的卡片上点「交给 AI」，把复制的那句话发给你的 AI Agent。"

Param(
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"

if (-not $Repo) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $Repo = Join-Path $ProjectRoot "third_party\GPT-SoVITS"
}

function Replace-Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Old,
        [Parameter(Mandatory = $true)][string]$New
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ($content.Contains($New)) {
        Write-Host "[OK] Already patched $Path"
    } elseif ($content.Contains($Old)) {
        [System.IO.File]::WriteAllText($Path, $content.Replace($Old, $New), $Utf8NoBom)
        Write-Host "[OK] Patched $Path"
    } else {
        Write-Host "[OK] Already patched $Path"
    }
}

function Replace-ChineseJiebaImport {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $newChinese = @"
import logging

try:
    import jieba_fast as jieba
    import jieba_fast.posseg as psg
except ModuleNotFoundError:
    import jieba
    import jieba.posseg as psg

jieba.setLogLevel(logging.CRITICAL)
"@

    if ($content.Contains($newChinese)) {
        Write-Host "[OK] Already patched $Path"
        return
    }

    $pattern = "import jieba_fast\r?\nimport logging\r?\n\r?\njieba_fast\.setLogLevel\(logging\.CRITICAL\)\r?\nimport jieba_fast\.posseg as psg"
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newChinese, 1)
    if ($patched -eq $content) {
        throw "Could not patch jieba_fast fallback in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-RefAudioLoad {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $newLoad = @"
        raw_audio_np, raw_sr = librosa.load(ref_audio_path, sr=None, mono=False)
        if raw_audio_np.ndim == 1:
            raw_audio_np = raw_audio_np[None, :]
        raw_audio = torch.from_numpy(np.ascontiguousarray(raw_audio_np)).to(self.configs.device).float()
"@

    if ($content.Contains($newLoad)) {
        Write-Host "[OK] Already patched $Path"
        return
    }

    $pattern = "        raw_audio, raw_sr = torchaudio\.load\(ref_audio_path\)\r?\n        raw_audio = raw_audio\.to\(self\.configs\.device\)\.float\(\)"
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newLoad, 1)
    if ($patched -eq $content) {
        throw "Could not patch torchaudio reference load in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-S1TrainerWindowsStrategy {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $oldTrainerArgs = @"
        devices=-1 if torch.cuda.is_available() else 1,
        benchmark=False,
        fast_dev_run=False,
        strategy=DDPStrategy(process_group_backend="nccl" if platform.system() != "Windows" else "gloo")
        if torch.cuda.is_available()
        else "auto",
"@
    $newTrainerArgs = @"
        devices=1 if platform.system() == "Windows" else (-1 if torch.cuda.is_available() else 1),
        benchmark=False,
        fast_dev_run=False,
        strategy="auto"
        if platform.system() == "Windows"
        else (
            DDPStrategy(process_group_backend="nccl")
            if torch.cuda.is_available()
            else "auto"
        ),
"@

    if ($content.Contains($newTrainerArgs)) {
        Write-Host "[OK] Already patched $Path"
        return
    }
    $pattern = '(?s)        devices=-1 if torch\.cuda\.is_available\(\) else 1,\r?\n        benchmark=False,\r?\n        fast_dev_run=False,\r?\n        strategy=DDPStrategy\(process_group_backend="nccl" if platform\.system\(\) != "Windows" else "gloo"\)\r?\n        if torch\.cuda\.is_available\(\)\r?\n        else "auto",'
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newTrainerArgs, 1)
    if ($patched -eq $content) {
        throw "Could not patch Windows S1 Trainer strategy in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-S1DataLoaderWorkers {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $newImport = @"
from torch.utils.data import DataLoader


def _loader_worker_options(num_workers):
    if num_workers and num_workers > 0:
        return {
            "num_workers": num_workers,
            "persistent_workers": True,
            "prefetch_factor": 16,
        }
    return {"num_workers": 0}


from AR.data.bucket_sampler import DistributedBucketSampler
"@
    $newTrainWorkers = "            **_loader_worker_options(self.num_workers),"
    $newValWorkers = "            **_loader_worker_options(self.num_workers),"

    if ($content.Contains($newImport) -and $content.Contains($newTrainWorkers)) {
        Write-Host "[OK] Already patched $Path"
        return
    }
    $patched = [System.Text.RegularExpressions.Regex]::Replace(
        $content,
        'from torch\.utils\.data import DataLoader\r?\n\r?\nfrom AR\.data\.bucket_sampler import DistributedBucketSampler',
        $newImport,
        1
    )
    $patched = [System.Text.RegularExpressions.Regex]::Replace(
        $patched,
        '            num_workers=self\.num_workers,\r?\n            persistent_workers=True,\r?\n            prefetch_factor=16,',
        $newTrainWorkers,
        1
    )
    $patched = [System.Text.RegularExpressions.Regex]::Replace(
        $patched,
        '            num_workers=max\(self\.num_workers, 12\),\r?\n            persistent_workers=True,\r?\n            prefetch_factor=16,',
        $newValWorkers,
        1
    )
    if ($patched -eq $content) {
        throw "Could not patch Windows S1 DataLoader workers in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-S1BucketSamplerSingleProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $oldReplicaBlock = @"
        if num_replicas is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            num_replicas = dist.get_world_size() if torch.cuda.is_available() else 1
        if rank is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            rank = dist.get_rank() if torch.cuda.is_available() else 0
            if torch.cuda.is_available():
                torch.cuda.set_device(rank)
"@
    $newReplicaBlock = @"
        distributed_ready = dist.is_available() and dist.is_initialized()
        if num_replicas is None:
            num_replicas = dist.get_world_size() if distributed_ready else 1
        if rank is None:
            rank = dist.get_rank() if distributed_ready else 0
            if distributed_ready and torch.cuda.is_available():
                torch.cuda.set_device(rank)
"@

    if ($content.Contains($newReplicaBlock)) {
        Write-Host "[OK] Already patched $Path"
        return
    }

    $pattern = '(?s)        if num_replicas is None:\r?\n            if not dist\.is_available\(\):\r?\n                raise RuntimeError\("Requires distributed package to be available"\)\r?\n            num_replicas = dist\.get_world_size\(\) if torch\.cuda\.is_available\(\) else 1\r?\n        if rank is None:\r?\n            if not dist\.is_available\(\):\r?\n                raise RuntimeError\("Requires distributed package to be available"\)\r?\n            rank = dist\.get_rank\(\) if torch\.cuda\.is_available\(\) else 0\r?\n            if torch\.cuda\.is_available\(\):\r?\n                torch\.cuda\.set_device\(rank\)'
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newReplicaBlock, 1)
    if ($patched -eq $content) {
        throw "Could not patch Windows S1 bucket sampler in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-S1CheckpointLoadWeightsOnly {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $oldFit = "    trainer.fit(model, data_module, ckpt_path=ckpt_path)"
    $newFit = "    trainer.fit(model, data_module, ckpt_path=ckpt_path, weights_only=False)"

    if ($content.Contains($newFit)) {
        Write-Host "[OK] Already patched $Path"
        return
    }
    if (-not $content.Contains($oldFit)) {
        throw "Could not patch Windows S1 checkpoint loading in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $content.Replace($oldFit, $newFit), $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-S2WindowsSingleProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $changed = $false

    $oldMain = @"
def main():
    if torch.cuda.is_available():
        n_gpus = torch.cuda.device_count()
    else:
        n_gpus = 1
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = str(randint(20000, 55555))

    mp.spawn(
        run,
        nprocs=n_gpus,
        args=(
            n_gpus,
            hps,
        ),
    )
"@
    $newMain = @"
def main():
    if os.name == "nt":
        n_gpus = 1
    elif torch.cuda.is_available():
        n_gpus = torch.cuda.device_count()
    else:
        n_gpus = 1
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = str(randint(20000, 55555))

    if os.name == "nt":
        run(0, n_gpus, hps)
    else:
        mp.spawn(
            run,
            nprocs=n_gpus,
            args=(
                n_gpus,
                hps,
            ),
        )
"@
    if (-not $content.Contains($newMain)) {
        $mainPattern = '(?s)def main\(\):\r?\n    if torch\.cuda\.is_available\(\):\r?\n        n_gpus = torch\.cuda\.device_count\(\)\r?\n    else:\r?\n        n_gpus = 1\r?\n    os\.environ\["MASTER_ADDR"\] = "localhost"\r?\n    os\.environ\["MASTER_PORT"\] = str\(randint\(20000, 55555\)\)\r?\n\r?\n    mp\.spawn\(\r?\n        run,\r?\n        nprocs=n_gpus,\r?\n        args=\(\r?\n            n_gpus,\r?\n            hps,\r?\n        \),\r?\n    \)'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $mainPattern, $newMain, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 main/spawn in $Path"
        }
        $content = $patched
        $changed = $true
    }

    $oldDist = @"
    dist.init_process_group(
        backend="gloo" if os.name == "nt" or not torch.cuda.is_available() else "nccl",
        init_method="env://?use_libuv=False",
        world_size=n_gpus,
        rank=rank,
    )
"@
    $newDist = @"
    if os.name != "nt":
        dist.init_process_group(
            backend="gloo" if not torch.cuda.is_available() else "nccl",
            init_method="env://?use_libuv=False",
            world_size=n_gpus,
            rank=rank,
        )
"@
    if (-not $content.Contains($newDist)) {
        $distPattern = '(?s)    dist\.init_process_group\(\r?\n        backend="gloo" if os\.name == "nt" or not torch\.cuda\.is_available\(\) else "nccl",\r?\n        init_method="env://\?use_libuv=False",\r?\n        world_size=n_gpus,\r?\n        rank=rank,\r?\n    \)'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $distPattern, $newDist, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 distributed init in $Path"
        }
        $content = $patched
        $changed = $true
    }

    $oldLoader = @"
        num_workers=5,
        shuffle=False,
        pin_memory=True,
        collate_fn=collate_fn,
        batch_sampler=train_sampler,
        persistent_workers=True,
        prefetch_factor=3,
"@
    $newLoader = @"
        num_workers=0 if os.name == "nt" else 5,
        shuffle=False,
        pin_memory=True,
        collate_fn=collate_fn,
        batch_sampler=train_sampler,
        persistent_workers=False if os.name == "nt" else True,
        prefetch_factor=None if os.name == "nt" else 3,
"@
    if (-not $content.Contains($newLoader)) {
        $loaderPattern = '(?s)        num_workers=5,\r?\n        shuffle=False,\r?\n        pin_memory=True,\r?\n        collate_fn=collate_fn,\r?\n        batch_sampler=train_sampler,\r?\n        persistent_workers=True,\r?\n        prefetch_factor=3,'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $loaderPattern, $newLoader, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 DataLoader workers in $Path"
        }
        $content = $patched
        $changed = $true
    }

    $oldDdp = @"
    if torch.cuda.is_available():
        net_g = DDP(net_g, device_ids=[rank], find_unused_parameters=True)
        net_d = DDP(net_d, device_ids=[rank], find_unused_parameters=True)
    else:
        net_g = net_g.to(device)
        net_d = net_d.to(device)
"@
    $newDdp = @"
    if torch.cuda.is_available() and os.name != "nt":
        net_g = DDP(net_g, device_ids=[rank], find_unused_parameters=True)
        net_d = DDP(net_d, device_ids=[rank], find_unused_parameters=True)
    elif not torch.cuda.is_available():
        net_g = net_g.to(device)
        net_d = net_d.to(device)
"@
    if (-not $content.Contains($newDdp)) {
        $ddpPattern = '(?s)    if torch\.cuda\.is_available\(\):\r?\n        net_g = DDP\(net_g, device_ids=\[rank\], find_unused_parameters=True\)\r?\n        net_d = DDP\(net_d, device_ids=\[rank\], find_unused_parameters=True\)\r?\n    else:\r?\n        net_g = net_g\.to\(device\)\r?\n        net_d = net_d\.to\(device\)'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $ddpPattern, $newDdp, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 DDP wrapping in $Path"
        }
        $content = $patched
        $changed = $true
    }

    $oldGLoad = @"
                net_g.module.load_state_dict(
                    torch.load(hps.train.pretrained_s2G, map_location="cpu", weights_only=False)["weight"],
                    strict=False,
                )
                if torch.cuda.is_available()
                else net_g.load_state_dict(
                    torch.load(hps.train.pretrained_s2G, map_location="cpu", weights_only=False)["weight"],
                    strict=False,
                ),
"@
    $newGLoad = @"
                (net_g.module if hasattr(net_g, "module") else net_g).load_state_dict(
                    torch.load(hps.train.pretrained_s2G, map_location="cpu", weights_only=False)["weight"],
                    strict=False,
                ),
"@
    if (-not $content.Contains($newGLoad)) {
        $gLoadPattern = '(?s)                net_g\.module\.load_state_dict\(\r?\n                    torch\.load\(hps\.train\.pretrained_s2G, map_location="cpu", weights_only=False\)\["weight"\],\r?\n                    strict=False,\r?\n                \)\r?\n                if torch\.cuda\.is_available\(\)\r?\n                else net_g\.load_state_dict\(\r?\n                    torch\.load\(hps\.train\.pretrained_s2G, map_location="cpu", weights_only=False\)\["weight"\],\r?\n                    strict=False,\r?\n                \),'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $gLoadPattern, $newGLoad, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 G pretrained load in $Path"
        }
        $content = $patched
        $changed = $true
    }

    $oldDLoad = @"
                net_d.module.load_state_dict(
                    torch.load(hps.train.pretrained_s2D, map_location="cpu", weights_only=False)["weight"], strict=False
                )
                if torch.cuda.is_available()
                else net_d.load_state_dict(
                    torch.load(hps.train.pretrained_s2D, map_location="cpu", weights_only=False)["weight"],
                ),
"@
    $newDLoad = @"
                (net_d.module if hasattr(net_d, "module") else net_d).load_state_dict(
                    torch.load(hps.train.pretrained_s2D, map_location="cpu", weights_only=False)["weight"],
                    strict=False,
                ),
"@
    if (-not $content.Contains($newDLoad)) {
        $dLoadPattern = '(?s)                net_d\.module\.load_state_dict\(\r?\n                    torch\.load\(hps\.train\.pretrained_s2D, map_location="cpu", weights_only=False\)\["weight"\], strict=False\r?\n                \)\r?\n                if torch\.cuda\.is_available\(\)\r?\n                else net_d\.load_state_dict\(\r?\n                    torch\.load\(hps\.train\.pretrained_s2D, map_location="cpu", weights_only=False\)\["weight"\],\r?\n                \),'
        $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $dLoadPattern, $newDLoad, 1)
        if ($patched -eq $content) {
            throw "Could not patch Windows S2 D pretrained load in $Path"
        }
        $content = $patched
        $changed = $true
    }

    if ($changed) {
        [System.IO.File]::WriteAllText($Path, $content, $Utf8NoBom)
        Write-Host "[OK] Patched $Path"
    } else {
        Write-Host "[OK] Already patched $Path"
    }
}

$toneSandhi = Join-Path $Repo "GPT_SoVITS\text\tone_sandhi.py"
$chinese = Join-Path $Repo "GPT_SoVITS\text\chinese.py"
$chinese2 = Join-Path $Repo "GPT_SoVITS\text\chinese2.py"
$tts = Join-Path $Repo "GPT_SoVITS\TTS_infer_pack\TTS.py"
$s1Train = Join-Path $Repo "GPT_SoVITS\s1_train.py"
$s1DataModule = Join-Path $Repo "GPT_SoVITS\AR\data\data_module.py"
$s1BucketSampler = Join-Path $Repo "GPT_SoVITS\AR\data\bucket_sampler.py"
$s2Train = Join-Path $Repo "GPT_SoVITS\s2_train.py"

Replace-Text `
    -Path $toneSandhi `
    -Old "import jieba_fast as jieba" `
    -New "try:`n    import jieba_fast as jieba`nexcept ModuleNotFoundError:`n    import jieba"

Replace-ChineseJiebaImport -Path $chinese
Replace-ChineseJiebaImport -Path $chinese2

Replace-RefAudioLoad -Path $tts
Replace-S1TrainerWindowsStrategy -Path $s1Train
Replace-S1DataLoaderWorkers -Path $s1DataModule
Replace-S1BucketSamplerSingleProcess -Path $s1BucketSampler
Replace-S1CheckpointLoadWeightsOnly -Path $s1Train
Replace-S2WindowsSingleProcess -Path $s2Train

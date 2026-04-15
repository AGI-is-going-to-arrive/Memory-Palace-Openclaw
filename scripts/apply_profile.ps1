param(
    [ValidateSet('macos', 'linux', 'windows', 'docker')]
    [string]$Platform = $(if ($env:OS -eq 'Windows_NT' -or [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { 'windows' } elseif ($IsLinux) { 'linux' } else { 'macos' }),

    [ValidateSet('a', 'b', 'c', 'd', 'A', 'B', 'C', 'D')]
    [string]$Profile = 'b',

    [string]$Target = ''
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$profileLower = $Profile.ToLower()

if ([string]::IsNullOrWhiteSpace($Target)) {
    $Target = Join-Path $projectRoot '.env'
}

$baseEnv = Join-Path $projectRoot '.env.example'
$overrideEnv = Join-Path $projectRoot ("deploy/profiles/{0}/profile-{1}.env" -f $Platform, $profileLower)

function Set-EnvValueInFile {
    param(
        [string]$FilePath,
        [string]$Key,
        [string]$Value
    )

    $lines = @()
    if (Test-Path $FilePath) {
        $lines = @(Get-Content -Path $FilePath)
    }

    $escaped = [regex]::Escape($Key)
    $updated = $false
    $newLines = [System.Collections.Generic.List[string]]::new()

    foreach ($line in $lines) {
        if ($line -match "^${escaped}=") {
            if (-not $updated) {
                $updated = $true
                [void]$newLines.Add("$Key=$Value")
            }
            continue
        }

        [void]$newLines.Add([string]$line)
    }

    if (-not $updated) {
        [void]$newLines.Add("$Key=$Value")
    }

    Set-Content -Path $FilePath -Value $newLines -Encoding utf8
}

function Dedupe-EnvKeys {
    param([string]$FilePath)

    if (-not (Test-Path $FilePath)) {
        return
    }

    $keys = Get-Content -Path $FilePath |
        Where-Object { $_ -match '^[A-Z0-9_]+=' } |
        ForEach-Object { ($_ -split '=', 2)[0] } |
        Group-Object |
        Where-Object { $_.Count -gt 1 } |
        Sort-Object Name

    foreach ($group in $keys) {
        $escaped = [regex]::Escape($group.Name)
        $lastLine = Get-Content -Path $FilePath |
            Where-Object { $_ -match "^${escaped}=" } |
            Select-Object -Last 1
        if (-not $lastLine) {
            continue
        }

        $value = ($lastLine -split '=', 2)[1]
        Set-EnvValueInFile -FilePath $FilePath -Key $group.Name -Value $value
    }
}

function Get-EnvValueFromFile {
    param(
        [string]$FilePath,
        [string]$Key
    )

    if (-not (Test-Path $FilePath)) {
        return ''
    }

    $escaped = [regex]::Escape($Key)
    $lastLine = Get-Content -Path $FilePath |
        Where-Object { $_ -match "^${escaped}=" } |
        Select-Object -Last 1

    if (-not $lastLine) {
        return ''
    }

    return ($lastLine -split '=', 2)[1]
}

function New-DockerMcpApiKey {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

function Get-ProfilePlaceholderLines {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string]$Profile
    )

    $requiredKeys = @(
        'RETRIEVAL_EMBEDDING_API_BASE',
        'RETRIEVAL_EMBEDDING_API_KEY',
        'RETRIEVAL_EMBEDDING_MODEL',
        'RETRIEVAL_RERANKER_API_BASE',
        'RETRIEVAL_RERANKER_API_KEY',
        'RETRIEVAL_RERANKER_MODEL'
    )
    if ($Profile -eq 'd') {
        $requiredKeys += @(
            'LLM_API_BASE',
            'LLM_API_KEY',
            'LLM_MODEL',
            'WRITE_GUARD_LLM_API_BASE',
            'WRITE_GUARD_LLM_API_KEY',
            'WRITE_GUARD_LLM_MODEL',
            'COMPACT_GIST_LLM_API_BASE',
            'COMPACT_GIST_LLM_API_KEY',
            'COMPACT_GIST_LLM_MODEL',
            'INTENT_LLM_API_BASE',
            'INTENT_LLM_API_KEY',
            'INTENT_LLM_MODEL'
        )
    }
    $markers = @(
        'replace-with-your-',
        '<your-',
        '127.0.0.1:port',
        'host.docker.internal:port',
        'https://<',
        'http://<'
    )

    $matches = @()
    foreach ($line in Get-Content -Path $FilePath) {
        if ($line -notmatch '^[A-Z0-9_]+=') {
            continue
        }
        $parts = $line -split '=', 2
        if ($parts.Count -ne 2) {
            continue
        }
        $key = $parts[0]
        $value = $parts[1].ToLowerInvariant()
        if ($key -notin $requiredKeys) {
            continue
        }
        foreach ($marker in $markers) {
            if ($value.Contains($marker)) {
                $matches += $line
                break
            }
        }
    }
    return $matches
}

if (-not (Test-Path $baseEnv)) {
    Write-Error "Missing base env template: $baseEnv"
    exit 1
}

if (-not (Test-Path $overrideEnv)) {
    Write-Error "Missing profile template: $overrideEnv"
    exit 1
}

Copy-Item -Path $baseEnv -Destination $Target -Force
Add-Content -Path $Target -Value "" -Encoding utf8
Add-Content -Path $Target -Value "# -----------------------------------------------------------------------------" -Encoding utf8
Add-Content -Path $Target -Value "# Appended profile overrides ($Platform/profile-$profileLower)" -Encoding utf8
Add-Content -Path $Target -Value "# -----------------------------------------------------------------------------" -Encoding utf8
Get-Content -Path $overrideEnv | Add-Content -Path $Target -Encoding utf8

if ($Platform -in @('macos', 'linux')) {
    $placeholder = if ($Platform -eq 'linux') {
        'DATABASE_URL=sqlite+aiosqlite:////home/<your-user>/memory_palace/agent_memory.db'
    } else {
        'DATABASE_URL=sqlite+aiosqlite:////Users/<your-user>/memory_palace/agent_memory.db'
    }
    if (Select-String -Path $Target -Pattern ([regex]::Escape($placeholder)) -Quiet) {
        $dbPath = (Join-Path $projectRoot 'demo.db') -replace '\\', '/'
        $dbUrl = 'DATABASE_URL=sqlite+aiosqlite:////' + $dbPath.TrimStart('/')
        Set-EnvValueInFile -FilePath $Target -Key 'DATABASE_URL' -Value $dbUrl.Substring('DATABASE_URL='.Length)
        Write-Host "[auto-fill] DATABASE_URL set to $dbPath"
    }
}

if ($Platform -eq 'windows') {
    $placeholder = 'DATABASE_URL=sqlite+aiosqlite:///C:/memory_palace/agent_memory.db'
    if (Select-String -Path $Target -Pattern ([regex]::Escape($placeholder)) -Quiet) {
        $dbPath = (Join-Path $projectRoot 'demo.db') -replace '\\', '/'
        if ($dbPath -match '^/([a-zA-Z])/(.*)$') {
            $drive = $Matches[1].ToUpperInvariant()
            $dbPath = "${drive}:/$($Matches[2])"
        }
        elseif ($dbPath -match '^/mnt/([a-zA-Z])/(.*)$') {
            $drive = $Matches[1].ToUpperInvariant()
            $dbPath = "${drive}:/$($Matches[2])"
        }
        elseif ($dbPath -notmatch '^[A-Za-z]:/') {
            $dbPath = 'C:/memory_palace/demo.db'
        }
        $dbUrl = 'DATABASE_URL=sqlite+aiosqlite:///' + $dbPath
        Set-EnvValueInFile -FilePath $Target -Key 'DATABASE_URL' -Value $dbUrl.Substring('DATABASE_URL='.Length)
        Write-Host "[auto-fill] DATABASE_URL set to $dbPath"
    }
}

if ($Platform -eq 'docker') {
    $currentApiKey = Get-EnvValueFromFile -FilePath $Target -Key 'MCP_API_KEY'
    if ([string]::IsNullOrWhiteSpace($currentApiKey)) {
        $generatedApiKey = New-DockerMcpApiKey
        Set-EnvValueInFile -FilePath $Target -Key 'MCP_API_KEY' -Value $generatedApiKey
        Write-Host "[auto-fill] MCP_API_KEY generated for docker profile"
    }
}

Dedupe-EnvKeys -FilePath $Target

if ($profileLower -in @('c', 'd')) {
    $placeholderLines = Get-ProfilePlaceholderLines -FilePath $Target -Profile $profileLower
    if ($placeholderLines.Count -gt 0) {
        Write-Error "Generated $Target, but Profile $($profileLower.ToUpperInvariant()) still contains placeholder provider values. Fill in real provider settings or use onboarding before treating this profile as ready.`n$($placeholderLines -join [Environment]::NewLine)"
        exit 3
    }
}

Write-Host "Generated $Target from $overrideEnv"

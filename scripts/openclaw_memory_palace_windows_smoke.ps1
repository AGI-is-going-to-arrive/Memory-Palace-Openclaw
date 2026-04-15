param(
  [ValidateSet("basic", "full", "dev")]
  [string]$Mode = "basic",

  [ValidateSet("a", "b", "c", "d")]
  [string]$Profile = "b",

  [ValidateSet("stdio", "sse")]
  [string]$Transport = "stdio",

  [string]$Config,
  [string]$SetupRoot,
  [string]$ModelEnv,
  [string]$ReportPath,
  [switch]$SkipAdvanced
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$SetupScript = Join-Path $RepoRoot "scripts/openclaw_memory_palace.py"

function Get-PythonLauncher {
  $candidates = @(
    @("py", "-3.14"),
    @("py", "-3.13"),
    @("py", "-3.12"),
    @("py", "-3.11"),
    @("py", "-3.10"),
    @("py", "-3"),
    @("python"),
    @("python3")
  )
  foreach ($candidate in $candidates) {
    $command = $candidate[0]
    $prefixArgs = @()
    if ($candidate.Length -gt 1) {
      $prefixArgs = $candidate[1..($candidate.Length - 1)]
    }
    try {
      & $command @prefixArgs -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)" *> $null
      if ($LASTEXITCODE -eq 0) {
        return ,@($command) + $prefixArgs
      }
    } catch {
      continue
    }
  }
  throw "Could not find a supported Python launcher. Tried: py -3.14, py -3.13, py -3.12, py -3.11, py -3.10, py -3, python, python3. Memory Palace requires Python 3.10-3.14."
}

function Invoke-RepoPython {
  param([string[]]$CommandArgs)
  $launcher = Get-PythonLauncher
  $command = $launcher[0]
  $prefixArgs = @()
  if ($launcher.Length -gt 1) {
    $prefixArgs = $launcher[1..($launcher.Length - 1)]
  }
  & $command @prefixArgs @CommandArgs
  if ($LASTEXITCODE -ne 0) {
    $renderedArgs = [string]::Join(" ", $prefixArgs + $CommandArgs)
    throw "Python command failed: $command $renderedArgs"
  }
}

function Invoke-RepoPythonCapture {
  param([string[]]$CommandArgs)
  $launcher = Get-PythonLauncher
  $command = $launcher[0]
  $prefixArgs = @()
  if ($launcher.Length -gt 1) {
    $prefixArgs = $launcher[1..($launcher.Length - 1)]
  }
  $output = & $command @prefixArgs @CommandArgs 2>&1
  if ($LASTEXITCODE -ne 0) {
    $joinedOutput = [string]::Join("`n", $output)
    $renderedArgs = [string]::Join(" ", $prefixArgs + $CommandArgs)
    throw "Python command failed: $command $renderedArgs`n$joinedOutput"
  }
  return ($output -join "`n")
}

function Import-DotEnv {
  param([string]$Path)
  if (-not $Path) {
    return
  }
  if (-not (Test-Path $Path)) {
    throw "Model env file not found: $Path"
  }
  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      return
    }
    $parts = $line.Split("=", 2)
    if ($parts.Length -ne 2) {
      return
    }
    $key = $parts[0].Trim()
    $value = $parts[1].Trim()
    if ($key) {
      Set-Item -Path "Env:$key" -Value $value
    }
  }
}

function Normalize-EmbeddingApiBase {
  param([string]$Value)
  if (-not $Value) { return $Value }
  $normalized = $Value.Trim()
  if ($normalized.EndsWith("/embeddings")) {
    return $normalized.Substring(0, $normalized.Length - "/embeddings".Length)
  }
  return $normalized
}

function Normalize-RerankerApiBase {
  param([string]$Value)
  if (-not $Value) { return $Value }
  $normalized = $Value.Trim()
  if ($normalized.EndsWith("/rerank")) {
    return $normalized.Substring(0, $normalized.Length - "/rerank".Length)
  }
  return $normalized
}

function Normalize-ChatApiBase {
  param([string]$Value)
  if (-not $Value) { return $Value }
  $normalized = $Value.Trim()
  $lower = $normalized.ToLowerInvariant()
  foreach ($suffix in @("/chat/completions", "/responses")) {
    if ($lower.EndsWith($suffix)) {
      return $normalized.Substring(0, $normalized.Length - $suffix.Length)
    }
  }
  return $normalized
}

function Invoke-JsonWarmup {
  param(
    [string]$BaseUrl,
    [string]$Endpoint,
    [hashtable]$Payload,
    [string]$ApiKey,
    [string]$Component,
    [int]$TimeoutSec = 60
  )
  if (-not $BaseUrl) {
    return
  }
  $target = "$($BaseUrl.TrimEnd('/'))$Endpoint"
  $startedAt = Get-Date
  try {
    $headers = @{ "Content-Type" = "application/json" }
    if ($ApiKey) {
      $headers["Authorization"] = "Bearer $ApiKey"
    }
    $body = $Payload | ConvertTo-Json -Depth 10
    $response = Invoke-WebRequest -Uri $target -Method Post -Body $body -Headers $headers -ContentType "application/json" -TimeoutSec $TimeoutSec
    $elapsed = ((Get-Date) - $startedAt).TotalSeconds
    Write-Host "[windows-smoke] prewarm $Component pass ${elapsed}s" -ForegroundColor DarkGray
    return $response
  } catch {
    $elapsed = ((Get-Date) - $startedAt).TotalSeconds
    Write-Host "[windows-smoke] prewarm $Component fail ${elapsed}s: $($_.Exception.Message)" -ForegroundColor DarkYellow
    return $null
  }
}

function Invoke-ProfileModelWarmup {
  param([string]$ProfileName)
  if ($ProfileName -notin @("c", "d")) {
    return
  }
  Invoke-JsonWarmup -Component "embedding" `
    -BaseUrl (Normalize-EmbeddingApiBase $env:RETRIEVAL_EMBEDDING_API_BASE) `
    -Endpoint "/embeddings" `
    -ApiKey $env:RETRIEVAL_EMBEDDING_API_KEY `
    -Payload @{
      model = $env:RETRIEVAL_EMBEDDING_MODEL
      input = "memory palace prewarm probe"
    } | Out-Null

  Invoke-JsonWarmup -Component "reranker" `
    -BaseUrl (Normalize-RerankerApiBase $env:RETRIEVAL_RERANKER_API_BASE) `
    -Endpoint "/rerank" `
    -ApiKey $env:RETRIEVAL_RERANKER_API_KEY `
    -Payload @{
      model = $env:RETRIEVAL_RERANKER_MODEL
      query = "memory palace prewarm probe"
      documents = @("probe document one", "probe document two")
    } | Out-Null

  Invoke-JsonWarmup -Component "write-guard-llm" `
    -BaseUrl (Normalize-ChatApiBase $env:WRITE_GUARD_LLM_API_BASE) `
    -Endpoint "/chat/completions" `
    -ApiKey $env:WRITE_GUARD_LLM_API_KEY `
    -Payload @{
      model = $env:WRITE_GUARD_LLM_MODEL
      temperature = 0
      messages = @(
        @{ role = "system"; content = "Reply with JSON only." },
        @{ role = "user"; content = "Return {`"ok`":true}." }
      )
    } | Out-Null

  Invoke-JsonWarmup -Component "compact-gist-llm" `
    -BaseUrl (Normalize-ChatApiBase $env:COMPACT_GIST_LLM_API_BASE) `
    -Endpoint "/chat/completions" `
    -ApiKey $env:COMPACT_GIST_LLM_API_KEY `
    -Payload @{
      model = $env:COMPACT_GIST_LLM_MODEL
      temperature = 0
      messages = @(
        @{ role = "system"; content = "Reply with JSON only." },
        @{ role = "user"; content = "Return {`"ok`":true}." }
      )
    } | Out-Null
}

function Get-OpenClawLauncher {
  $native = Get-Command "openclaw" -ErrorAction SilentlyContinue
  if ($native) {
    return @{
      Command = "openclaw"
      PrefixArgs = @()
    }
  }

  if ($env:OPENCLAW_ALLOW_LOCAL_MODULE_FALLBACK -eq "1") {
    $localModule = Join-Path $RepoRoot "node_modules/openclaw/openclaw.mjs"
    if (Test-Path $localModule) {
      return @{
        Command = "node"
        PrefixArgs = @($localModule)
      }
    }
  }

  if ($env:OPENCLAW_ALLOW_NPX_FALLBACK -eq "1") {
    $npx = Get-Command "npx" -ErrorAction SilentlyContinue
    if ($npx) {
      return @{
        Command = "npx"
        PrefixArgs = @("--yes", "openclaw")
      }
    }
  }

  throw "Could not find OpenClaw CLI. Install `openclaw`, or explicitly opt in to OPENCLAW_ALLOW_LOCAL_MODULE_FALLBACK=1 / OPENCLAW_ALLOW_NPX_FALLBACK=1 for maintainer-only fallback."
}

function Invoke-OpenClaw {
  param([string[]]$CommandArgs)
  $launcher = Get-OpenClawLauncher
  $prefixArgs = @($launcher.PrefixArgs)
  & $launcher.Command @prefixArgs @CommandArgs
  if ($LASTEXITCODE -ne 0) {
    $renderedArgs = [string]::Join(" ", $prefixArgs + $CommandArgs)
    throw "Command failed: $($launcher.Command) $renderedArgs"
  }
}

function Invoke-OpenClawCapture {
  param(
    [string[]]$CommandArgs,
    [switch]$AllowFailure
  )
  $launcher = Get-OpenClawLauncher
  $prefixArgs = @($launcher.PrefixArgs)
  $output = & $launcher.Command @prefixArgs @CommandArgs 2>&1
  if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
    $joinedOutput = [string]::Join("`n", $output)
    $renderedArgs = [string]::Join(" ", $prefixArgs + $CommandArgs)
    throw "Command failed: $($launcher.Command) $renderedArgs`n$joinedOutput"
  }
  return ($output -join "`n")
}

function Convert-JsonPayload {
  param(
    [string]$Text,
    [string]$Context
  )
  $trimmed = [string]($Text ?? "").Trim()
  if (-not $trimmed) {
    throw "$Context returned empty stdout"
  }
  $candidates = New-Object "System.Collections.Generic.List[string]"
  $lines = $trimmed -split "`r?`n"
  for ($lineIndex = $lines.Length - 1; $lineIndex -ge 0; $lineIndex--) {
    $candidate = [string]($lines[$lineIndex] ?? "").Trim()
    if (
      $candidate -and (
        ($candidate.StartsWith("{") -and $candidate.EndsWith("}")) -or
        ($candidate.StartsWith("[") -and $candidate.EndsWith("]"))
      ) -and -not $candidates.Contains($candidate)
    ) {
      $candidates.Add($candidate)
    }
  }
  $candidates.Add($trimmed)
  for ($index = $trimmed.Length - 1; $index -ge 0; $index--) {
    $char = $trimmed[$index]
    if ($char -eq "{" -or $char -eq "[") {
      $candidate = $trimmed.Substring($index).Trim()
      if (-not $candidates.Contains($candidate)) {
        $candidates.Add($candidate)
      }
    }
  }
  foreach ($candidate in $candidates) {
    try {
      return $candidate | ConvertFrom-Json -Depth 100
    } catch {
      continue
    }
  }
  throw "$Context returned invalid JSON:`n$trimmed"
}

function Invoke-RepoPythonJson {
  param(
    [string[]]$CommandArgs,
    [string]$Context
  )
  $output = Invoke-RepoPythonCapture -CommandArgs $CommandArgs
  return Convert-JsonPayload -Text $output -Context $Context
}

function Invoke-OpenClawJson {
  param(
    [string[]]$CommandArgs,
    [string]$Context,
    [switch]$AllowFailure
  )
  $output = Invoke-OpenClawCapture -CommandArgs $CommandArgs -AllowFailure:$AllowFailure
  return Convert-JsonPayload -Text $output -Context $Context
}

function Assert-Condition {
  param(
    [bool]$Condition,
    [string]$Message
  )
  if (-not $Condition) {
    throw $Message
  }
}

function Convert-PathToFileUri {
  param([string]$Path)
  $resolved = (Resolve-Path $Path).ProviderPath
  return ([System.Uri]::new($resolved)).AbsoluteUri
}

function New-TinyPngFile {
  param([string]$Path)
  $pngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5W8l8AAAAASUVORK5CYII="
  [System.IO.File]::WriteAllBytes($Path, [System.Convert]::FromBase64String($pngBase64))
}

function Get-JsonStringProperty {
  param(
    [object]$Payload,
    [string]$Name
  )
  if ($null -eq $Payload) {
    return ""
  }
  $property = $Payload.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return ""
  }
  return [string]($property.Value ?? "")
}

function Get-VisualTarget {
  param([object]$Payload)
  $path = Get-JsonStringProperty -Payload $Payload -Name "path"
  if ($path) {
    return $path
  }
  $uri = Get-JsonStringProperty -Payload $Payload -Name "uri"
  if ($uri) {
    return $uri
  }
  throw "Expected payload to expose either path or uri."
}

function Save-ValidationReport {
  param(
    [string]$Path,
    [hashtable]$Payload
  )
  if (-not $Path) {
    return
  }
  $parent = Split-Path -Parent $Path
  if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  ($Payload | ConvertTo-Json -Depth 100) | Out-File -FilePath $Path -Encoding utf8
}

function Sanitize-ReportValue {
  param([object]$Value)
  if ($null -eq $Value) {
    return $null
  }
  if ($Value -is [string]) {
    $text = [string]$Value
    if ($text -match '^file:') {
      return "file://<redacted>"
    }
    if ($text -match '^[A-Za-z]:[\\/]') {
      return "<redacted-path>"
    }
    if ($text -match '^/') {
      return "<redacted-path>"
    }
    return $text
  }
  if ($Value -is [System.Collections.IDictionary]) {
    $sanitizedMap = [ordered]@{}
    foreach ($key in $Value.Keys) {
      $sanitizedMap[$key] = Sanitize-ReportValue -Value $Value[$key]
    }
    return $sanitizedMap
  }
  if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
    $items = @()
    foreach ($item in $Value) {
      $items += ,(Sanitize-ReportValue -Value $item)
    }
    return $items
  }
  return $Value
}

function Save-SanitizedValidationReport {
  param(
    [string]$Path,
    [hashtable]$Payload
  )
  if (-not $Path) {
    return
  }
  $sanitized = Sanitize-ReportValue -Value $Payload
  Save-ValidationReport -Path $Path -Payload $sanitized
}

Import-DotEnv -Path $ModelEnv
if ($Profile -in @("c", "d") -and -not $env:OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_TIMEOUT_SEC) {
  $env:OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_TIMEOUT_SEC = "20"
}
Invoke-ProfileModelWarmup -ProfileName $Profile

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mp-openclaw-win-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$resolvedConfigPath = if ($Config) { $Config } else { Join-Path $tempRoot "openclaw.json" }
$stateDir = Join-Path $tempRoot "state"
$resolvedSetupRoot = if ($SetupRoot) { $SetupRoot } else { Join-Path $tempRoot "memory-palace-runtime" }
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$env:OPENCLAW_CONFIG_PATH = $resolvedConfigPath
$env:OPENCLAW_STATE_DIR = $stateDir

$report = [ordered]@{
  ok = $false
  profile = $Profile
  mode = $Mode
  transport = $Transport
  configPath = $resolvedConfigPath
  setupRoot = $resolvedSetupRoot
  tempRoot = $tempRoot
  checks = [ordered]@{}
  advanced = [ordered]@{}
}

try {
  $setupArgs = @(
    $SetupScript,
    "setup",
    "--config", $resolvedConfigPath,
    "--setup-root", $resolvedSetupRoot,
    "--mode", $Mode,
    "--profile", $Profile,
    "--transport", $Transport,
    "--json"
  )
  if ($Profile -in @("c", "d")) {
    $setupArgs += "--strict-profile"
  }

  Write-Host "[windows-smoke] setup" -ForegroundColor Cyan
  $setupPayload = Invoke-RepoPythonJson -CommandArgs $setupArgs -Context "windows smoke setup"
  $report["setup"] = [ordered]@{
    requestedProfile = Get-JsonStringProperty -Payload $setupPayload -Name "requested_profile"
    effectiveProfile = Get-JsonStringProperty -Payload $setupPayload -Name "effective_profile"
    fallbackApplied = [bool]($setupPayload.fallback_applied)
    dryRun = [bool]($setupPayload.dry_run)
    warnings = @($setupPayload.warnings)
    summary = Get-JsonStringProperty -Payload $setupPayload -Name "summary"
  }

  Assert-Condition ([bool]$setupPayload.ok) "Setup returned ok=false."
  Assert-Condition ((Get-JsonStringProperty -Payload $setupPayload -Name "requested_profile") -eq $Profile) "Setup requested profile mismatch."
  Assert-Condition ((Get-JsonStringProperty -Payload $setupPayload -Name "effective_profile") -eq $Profile) "Setup effective profile mismatch."
  Assert-Condition (-not [bool]($setupPayload.fallback_applied)) "Setup unexpectedly fell back to a different profile."

  Write-Host "[windows-smoke] OPENCLAW_CONFIG_PATH=$env:OPENCLAW_CONFIG_PATH" -ForegroundColor DarkGray
  if (Test-Path $resolvedConfigPath) {
    Write-Host "[windows-smoke] config exists" -ForegroundColor DarkGray
    Get-Content $resolvedConfigPath | Select-Object -First 80 | ForEach-Object {
      Write-Host $_ -ForegroundColor DarkGray
    }
  } else {
    Write-Host "[windows-smoke] temp tree" -ForegroundColor DarkYellow
    Get-ChildItem -Recurse $tempRoot | ForEach-Object { Write-Host $_.FullName -ForegroundColor DarkYellow }
    throw "Expected config file was not created: $resolvedConfigPath"
  }

  Write-Host "[windows-smoke] openclaw config validate --json" -ForegroundColor Cyan
  $report.checks["config_validate"] = Invoke-OpenClawJson -CommandArgs @("config", "validate", "--json") -Context "openclaw config validate"

  $commandSpecs = @(
    @{ Name = "plugins_info"; Args = @("plugins", "inspect", "memory-palace", "--json") },
    @{ Name = "status_slot"; Args = @("status", "--json") },
    @{ Name = "memory_status"; Args = @("memory-palace", "status", "--json") },
    @{ Name = "memory_verify"; Args = @("memory-palace", "verify", "--json") },
    @{ Name = "memory_doctor"; Args = @("memory-palace", "doctor", "--json") },
    @{ Name = "memory_smoke"; Args = @("memory-palace", "smoke", "--json") }
  )

  foreach ($spec in $commandSpecs) {
    $renderedCommand = [string]::Join(" ", $spec.Args)
    Write-Host "[windows-smoke] openclaw $renderedCommand" -ForegroundColor Cyan
    $report.checks[$spec.Name] = Invoke-OpenClawJson -CommandArgs $spec.Args -Context "openclaw $renderedCommand"
  }

  Assert-Condition ((Get-JsonStringProperty -Payload $report.checks["plugins_info"] -Name "status") -eq "loaded") "Plugin is not loaded."
  $toolNames = @($report.checks["plugins_info"].toolNames)
  Assert-Condition ($toolNames -contains "memory_search") "memory_search tool is missing from plugin info."
  Assert-Condition ($toolNames -contains "memory_get") "memory_get tool is missing from plugin info."
  Assert-Condition ((Get-JsonStringProperty -Payload $report.checks["status_slot"].memoryPlugin -Name "slot") -eq "memory-palace") "Memory slot is not assigned to memory-palace."
  Assert-Condition ([bool]($report.checks["memory_status"].status.ok)) "memory-palace status did not report ok=true."
  Assert-Condition ([bool]($report.checks["memory_verify"].ok)) "memory-palace verify did not report ok=true."
  Assert-Condition ([bool]($report.checks["memory_doctor"].ok)) "memory-palace doctor did not report ok=true."
  Assert-Condition ([bool]($report.checks["memory_smoke"].ok)) "memory-palace smoke did not report ok=true."

  if (-not $SkipAdvanced) {
    $token = "windows-native-validation-$Profile-$([Guid]::NewGuid().ToString("N").Substring(0, 12))"
    $imagePath = Join-Path $tempRoot "$token.png"
    New-TinyPngFile -Path $imagePath
    $mediaRef = Convert-PathToFileUri -Path $imagePath
    $summary = "windows native validation board $token"
    $ocr = "$token whiteboard launch plan"
    $scene = "$token release board"

    Write-Host "[windows-smoke] openclaw memory-palace store-visual --json" -ForegroundColor Cyan
    $storePayload = Invoke-OpenClawJson -CommandArgs @(
      "memory-palace",
      "store-visual",
      "--media-ref", $mediaRef,
      "--summary", $summary,
      "--ocr", $ocr,
      "--scene", $scene,
      "--why-relevant", "windows native validation advanced probe",
      "--json"
    ) -Context "openclaw memory-palace store-visual"
    $storeTarget = Get-VisualTarget -Payload $storePayload
    Assert-Condition ([bool]$storePayload.ok) "Initial store-visual did not report ok=true."
    Assert-Condition ((Get-JsonStringProperty -Payload $storePayload -Name "runtime_visual_probe") -eq "cli_store_visual_only") "store-visual runtime_visual_probe mismatch."

    $indexPayload = Invoke-OpenClawJson -CommandArgs @("memory-palace", "index", "--wait", "--json") -Context "openclaw memory-palace index --wait"
    if ($null -ne $indexPayload.result) {
      Assert-Condition ([bool]($indexPayload.result.ok)) "index --wait did not report result.ok=true."
    }

    $searchPayload = Invoke-OpenClawJson -CommandArgs @("memory-palace", "search", $token, "--json") -Context "openclaw memory-palace search advanced probe"
    $searchResults = @($searchPayload.results)
    Assert-Condition ($searchResults.Count -gt 0) "Advanced search returned no results."
    $searchMatched = $false
    foreach ($item in $searchResults) {
      if ((Get-JsonStringProperty -Payload $item -Name "path") -eq $storePayload.path -or (Get-JsonStringProperty -Payload $item -Name "uri") -eq $storePayload.uri) {
        $searchMatched = $true
        break
      }
    }
    Assert-Condition $searchMatched "Advanced search results did not include the stored visual record."

    $getPayload = Invoke-OpenClawJson -CommandArgs @("memory-palace", "get", $storeTarget, "--json") -Context "openclaw memory-palace get advanced probe"
    $getText = Get-JsonStringProperty -Payload $getPayload -Name "text"
    Assert-Condition ($getText -like "*$token*") "Advanced get output is missing the probe token."
    Assert-Condition ($getText -like "*$summary*") "Advanced get output is missing the stored summary."

    $rejectPayload = Invoke-OpenClawJson -CommandArgs @(
      "memory-palace",
      "store-visual",
      "--media-ref", $mediaRef,
      "--summary", $summary,
      "--ocr", $ocr,
      "--scene", $scene,
      "--duplicate-policy", "reject",
      "--json"
    ) -Context "openclaw memory-palace store-visual duplicate reject" -AllowFailure
    $rejectReason = Get-JsonStringProperty -Payload $rejectPayload -Name "reason"
    $rejectMessage = Get-JsonStringProperty -Payload $rejectPayload -Name "message"
    Assert-Condition (-not [bool]$rejectPayload.ok) "duplicate-policy=reject unexpectedly returned ok=true."
    Assert-Condition (
      [bool]$rejectPayload.rejected -or
      ($rejectReason -eq "write_guard_blocked") -or
      ($rejectMessage -like "*write_guard blocked*")
    ) "duplicate-policy=reject did not report the expected guard-blocked rejection."

    $newPayload = Invoke-OpenClawJson -CommandArgs @(
      "memory-palace",
      "store-visual",
      "--media-ref", $mediaRef,
      "--summary", $summary,
      "--ocr", $ocr,
      "--scene", $scene,
      "--duplicate-policy", "new",
      "--json"
    ) -Context "openclaw memory-palace store-visual duplicate new"
    $newTarget = Get-VisualTarget -Payload $newPayload
    Assert-Condition ([bool]$newPayload.ok) "duplicate-policy=new did not report ok=true."
    Assert-Condition ($newTarget -ne $storeTarget) "duplicate-policy=new did not create a distinct target."

    $newGetPayload = Invoke-OpenClawJson -CommandArgs @("memory-palace", "get", $newTarget, "--json") -Context "openclaw memory-palace get duplicate new"
    $newGetText = Get-JsonStringProperty -Payload $newGetPayload -Name "text"
    Assert-Condition ($newGetText -like "*duplicate_policy: new*") "duplicate-policy=new record is missing duplicate_policy marker."
    Assert-Condition ($newGetText -like "*duplicate_variant: new-*") "duplicate-policy=new record is missing duplicate_variant marker."

    $report.advanced = [ordered]@{
      token = $token
      mediaRef = $mediaRef
      storeVisual = [ordered]@{
        ok = [bool]$storePayload.ok
        target = $storeTarget
      }
      index = [ordered]@{
        ok = if ($null -ne $indexPayload.result) { [bool]($indexPayload.result.ok) } else { $true }
      }
      search = [ordered]@{
        ok = $true
        resultCount = $searchResults.Count
      }
      get = [ordered]@{
        ok = $true
      }
      duplicateReject = [ordered]@{
        ok = [bool]$rejectPayload.ok
        reason = $rejectReason
        message = $rejectMessage
      }
      duplicateNew = [ordered]@{
        ok = [bool]$newPayload.ok
        target = $newTarget
      }
    }
  }

  $report.ok = $true
  Write-Host "[windows-smoke] config=$resolvedConfigPath" -ForegroundColor DarkGray
  Write-Host "[windows-smoke] stateDir=$stateDir" -ForegroundColor DarkGray
  Write-Host "[windows-smoke] setupRoot=$resolvedSetupRoot" -ForegroundColor DarkGray
  Write-Host "[windows-smoke] PASS" -ForegroundColor Green
}
catch {
  $report["error"] = $_.Exception.Message
  Write-Host "[windows-smoke] FAIL: $($_.Exception.Message)" -ForegroundColor Red
  throw
}
finally {
  Save-SanitizedValidationReport -Path $ReportPath -Payload $report
  if ($report.ok -and (Test-Path $tempRoot)) {
    try {
      Remove-Item -Recurse -Force $tempRoot
    } catch {
      Write-Host "[windows-smoke] temp cleanup skipped: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }
}

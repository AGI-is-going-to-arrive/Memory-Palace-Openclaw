const HOST_BRIDGE_SENSITIVE_ASSIGNMENT_PATTERNS = [
  /\b(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+/iu,
  /\b(x-mcp-api-key\s*[:=]\s*)[^\s,;]+/iu,
  /\b(api[-_ ]?key\s*[:=]\s*)[^\s,;]+/iu,
  /\b(access[-_ ]?key\s*[:=]\s*)[^\s,;]+/iu,
  /\b(client[-_ ]?secret\s*[:=]\s*)[^\s,;]+/iu,
  /\b(secret\s*[:=]\s*)[^\s,;]+/iu,
  /\b(password|passwd)\s*[:=]\s*[^\s,;]+/iu,
  /\b(token\s*[:=]\s*)[^\s,;]+/iu,
  /\b(refresh[-_ ]?token\s*[:=]\s*)[^\s,;]+/iu,
  /\b(session[-_ ]?token\s*[:=]\s*)[^\s,;]+/iu,
] as const;

export const HOST_BRIDGE_SENSITIVE_PATTERNS = [
  ...HOST_BRIDGE_SENSITIVE_ASSIGNMENT_PATTERNS,
  /\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b/u,
  /\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b/u,
  /\b(?:xox[baprs]-[A-Za-z0-9-]{10,})\b/u,
  /\b(?:AKIA|ASIA|AIDA|AROA|AGPA|AIPA)[A-Z0-9]{16}\b/u,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b/u,
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/iu,
  /-----END [A-Z0-9 ]*PRIVATE KEY-----/iu,
  /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/iu,
] as const;

const HOST_BRIDGE_SENSITIVE_CONTEXT_PATTERN =
  /\b(api[-_ ]?key|access[-_ ]?key|client[-_ ]?secret|secret|password|passwd|token|authorization|bearer|private[-_ ]?key|ssh key|session[-_ ]?token|refresh[-_ ]?token)\b/iu;
const HOST_BRIDGE_SECRET_CANDIDATE_PATTERN = /[A-Za-z0-9+/=_-]{20,}/g;
const HOST_BRIDGE_URL_PREFIX_PATTERN = /^(?:https?:\/\/|file:|mailto:)/iu;

function shannonEntropy(value: string): number {
  if (!value) {
    return 0;
  }
  const counts = new Map<string, number>();
  for (const char of value) {
    counts.set(char, (counts.get(char) ?? 0) + 1);
  }
  let entropy = 0;
  for (const count of counts.values()) {
    const probability = count / value.length;
    entropy -= probability * Math.log2(probability);
  }
  return entropy;
}

function countCharacterClasses(value: string): number {
  let classes = 0;
  if (/[a-z]/.test(value)) {
    classes += 1;
  }
  if (/[A-Z]/.test(value)) {
    classes += 1;
  }
  if (/\d/.test(value)) {
    classes += 1;
  }
  if (/[^A-Za-z0-9]/.test(value)) {
    classes += 1;
  }
  return classes;
}

function looksHighEntropySecretCandidate(text: string): boolean {
  const candidates = text.match(HOST_BRIDGE_SECRET_CANDIDATE_PATTERN) ?? [];
  for (const candidate of candidates) {
    const trimmed = candidate.trim();
    if (
      trimmed.length < 20 ||
      HOST_BRIDGE_URL_PREFIX_PATTERN.test(trimmed) ||
      /^[a-z][a-z0-9_-]{0,31}$/u.test(trimmed)
    ) {
      continue;
    }
    if (/^[A-Fa-f0-9]{32,128}$/u.test(trimmed)) {
      return true;
    }
    const entropy = shannonEntropy(trimmed);
    if (trimmed.length >= 24 && entropy >= 3.3 && countCharacterClasses(trimmed) >= 3) {
      return true;
    }
  }
  return false;
}

export function isSensitiveHostBridgeText(text: string): boolean {
  const normalized = String(text || "").trim();
  if (!normalized) {
    return false;
  }
  if (HOST_BRIDGE_SENSITIVE_PATTERNS.some((pattern) => pattern.test(normalized))) {
    return true;
  }
  if (!HOST_BRIDGE_SENSITIVE_CONTEXT_PATTERN.test(normalized)) {
    return false;
  }
  return looksHighEntropySecretCandidate(normalized);
}

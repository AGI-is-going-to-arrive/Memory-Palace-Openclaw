import type { PluginConfig } from "./types.js";

const ROOT_MARKER = "__root__";
const RESERVED_SEGMENT_PREFIX = "%24mp%24";
const INVALID_PERCENT_ESCAPE_PATTERN = /%(?![0-9A-Fa-f]{2})/u;
const MAX_URI_LENGTH = 4096;
const MAX_URI_PATH_LENGTH = 2048;
const MAX_URI_DEPTH = 128;
const WINDOWS_ABSOLUTE_PATH_PATTERN = /^[a-zA-Z]:($|\/)/;

function encodeSegment(value: string): string {
  const encoded = encodeURIComponent(value);
  return encoded === ROOT_MARKER ? `${RESERVED_SEGMENT_PREFIX}${encoded}` : encoded;
}

function decodeSegment(value: string): string {
  const raw = value.startsWith(RESERVED_SEGMENT_PREFIX)
    ? value.slice(RESERVED_SEGMENT_PREFIX.length)
    : value;
  try {
    const decoded = decodeURIComponent(raw).normalize("NFC");
    return decoded.startsWith(RESERVED_SEGMENT_PREFIX)
      ? decoded.slice(RESERVED_SEGMENT_PREFIX.length)
      : decoded;
  } catch {
    return raw.normalize("NFC");
  }
}

function decodeSegmentStrict(value: string): string {
  const raw = value.startsWith(RESERVED_SEGMENT_PREFIX)
    ? value.slice(RESERVED_SEGMENT_PREFIX.length)
    : value;
  if (INVALID_PERCENT_ESCAPE_PATTERN.test(raw)) {
    throw new Error("URI path contains invalid percent escapes.");
  }
  const decoded = decodeURIComponent(raw).normalize("NFC");
  return decoded.startsWith(RESERVED_SEGMENT_PREFIX)
    ? decoded.slice(RESERVED_SEGMENT_PREFIX.length)
    : decoded;
}

function validateUriPath(pathValue: string): string {
  let decoded = "";
  try {
    decoded = decodeSegmentStrict(pathValue);
  } catch {
    throw new Error("URI path contains invalid percent escapes.");
  }
  const normalized = decoded.replaceAll("\\", "/").trim().replace(/^\/+|\/+$/g, "");
  if (!normalized) {
    return "";
  }
  if (normalized.includes("\0")) {
    throw new Error("URI path contains invalid characters.");
  }
  if (WINDOWS_ABSOLUTE_PATH_PATTERN.test(normalized)) {
    throw new Error("URI path looks like a Windows absolute path.");
  }
  if (normalized.length > MAX_URI_PATH_LENGTH) {
    throw new Error(`URI path is too long (${normalized.length} > ${MAX_URI_PATH_LENGTH}).`);
  }
  const segments = normalized.split("/");
  if (segments.length > MAX_URI_DEPTH) {
    throw new Error(`URI path is too deep (${segments.length} > ${MAX_URI_DEPTH}).`);
  }
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new Error("URI path contains invalid traversal segments.");
  }
  return normalized;
}

export function splitUri(
  uri: string,
  defaultDomain: string,
): { domain: string; path: string } {
  const trimmed = uri.trim().normalize("NFC");
  if (trimmed.length > MAX_URI_LENGTH) {
    throw new Error(`URI is too long (${trimmed.length} > ${MAX_URI_LENGTH}).`);
  }
  const normalizedDefaultDomain = defaultDomain.trim().toLowerCase();
  const matched = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/(.*)$/.exec(trimmed);
  if (!matched) {
    return { domain: normalizedDefaultDomain, path: validateUriPath(trimmed) };
  }
  const domain = decodeSegmentStrict(matched[1]).trim().toLowerCase();
  const path = validateUriPath(matched[2]);
  if (!domain) {
    throw new Error("URI domain must not be empty.");
  }
  return {
    domain,
    path,
  };
}

export function uriToVirtualPath(uri: string, mapping: PluginConfig["mapping"]): string {
  const { domain, path: uriPath } = splitUri(uri, mapping.defaultDomain);
  const normalizedPath = uriPath
    .split("/")
    .filter(Boolean)
    .map(encodeSegment)
    .join("/");
  if (!normalizedPath) {
    return `${mapping.virtualRoot}/${encodeSegment(domain)}/${ROOT_MARKER}.md`;
  }
  return `${mapping.virtualRoot}/${encodeSegment(domain)}/${normalizedPath}.md`;
}

export function virtualPathToUri(pathValue: string, mapping: PluginConfig["mapping"]): string {
  const trimmed = pathValue.trim().replaceAll("\\", "/").replace(/^\.?\//, "");
  const withoutRoot = trimmed.startsWith(`${mapping.virtualRoot}/`)
    ? trimmed.slice(mapping.virtualRoot.length + 1)
    : trimmed;
  const normalized = withoutRoot.endsWith(".md") ? withoutRoot.slice(0, -3) : withoutRoot;
  const rawParts = normalized.split("/").filter(Boolean);
  const rawDomain = rawParts.shift();
  const domain = rawDomain ? decodeSegment(rawDomain) : mapping.defaultDomain;
  if (rawParts.length === 0 || (rawParts.length === 1 && rawParts[0] === ROOT_MARKER)) {
    return `${domain}://`;
  }
  const parts = rawParts.map(decodeSegment);
  return `${domain}://${parts.join("/")}`;
}

export function resolvePathLikeToUri(pathOrUri: string, mapping: PluginConfig["mapping"]): string {
  if (pathOrUri.includes("://")) {
    const { domain, path } = splitUri(pathOrUri, mapping.defaultDomain);
    return `${domain}://${path}`;
  }
  return virtualPathToUri(pathOrUri, mapping);
}

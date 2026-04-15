import type {
  DiagnosticCheck,
  DiagnosticReport,
  DiagnosticStatus,
  PluginConfig,
  PluginRuntimeSnapshot,
} from "./types.js";
import { isRecord, readString } from "./utils.js";

export function resolveReportStatus(
  checks: DiagnosticCheck[],
  ignoredWarnIds: ReadonlySet<string> = new Set(),
): DiagnosticStatus {
  if (checks.some((entry) => entry.status === "fail")) {
    return "fail";
  }
  if (checks.some((entry) => entry.status === "warn" && !ignoredWarnIds.has(entry.id))) {
    return "warn";
  }
  return "pass";
}

export function normalizeDiagnosticCodePart(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function extractDiagnosticDegradeReasons(value: unknown): string[] {
  if (!isRecord(value)) {
    return [];
  }
  const direct = value.degrade_reasons;
  if (Array.isArray(direct)) {
    return direct.filter((entry): entry is string => typeof entry === "string" && entry.trim().length > 0);
  }
  const metadata = isRecord(value.metadata) ? value.metadata : null;
  const nested = metadata?.degrade_reasons;
  if (Array.isArray(nested)) {
    return nested.filter((entry): entry is string => typeof entry === "string" && entry.trim().length > 0);
  }
  return [];
}

export function resolveDiagnosticCause(check: DiagnosticCheck): string | undefined {
  if (check.cause?.trim()) {
    return check.cause.trim();
  }

  const degradeReasons = extractDiagnosticDegradeReasons(check.details);
  if (degradeReasons.length > 0) {
    return degradeReasons[0];
  }

  const normalizedMessage = check.message.trim().toLowerCase();
  if (check.id === "transport-health") {
    if (/(401|403|unauthorized|forbidden|auth)/i.test(normalizedMessage)) {
      return "transport_auth_failure";
    }
    if (/(certificate verify failed|tls|ssl)/i.test(normalizedMessage)) {
      return "transport_tls_failure";
    }
    if (/(econnrefused|connect failed|refused)/i.test(normalizedMessage)) {
      return "transport_connect_failed";
    }
    if (/(timeout|timed out)/i.test(normalizedMessage)) {
      return "transport_timeout";
    }
  }

  if (check.id === "read-probe" && normalizedMessage.includes("no readable path")) {
    return "read_probe_target_missing";
  }
  if (check.id === "search-probe" && normalizedMessage.includes("returned no hits")) {
    return "search_probe_empty";
  }
  if (check.id === "sse-url" && check.status !== "pass") {
    return "sse_url_missing";
  }
  if (check.id === "stdio-command" && check.status !== "pass") {
    return "stdio_command_missing";
  }
  return undefined;
}

export function enrichDiagnosticCheck(check: DiagnosticCheck): DiagnosticCheck {
  const cause = resolveDiagnosticCause(check);
  return {
    ...check,
    ...(check.code?.trim()
      ? { code: check.code.trim() }
      : { code: `${normalizeDiagnosticCodePart(check.id)}_${normalizeDiagnosticCodePart(check.status)}` }),
    ...(cause ? { cause } : {}),
  };
}

export function buildDiagnosticReport(
  command: DiagnosticReport["command"],
  configTransport: PluginConfig["transport"],
  checks: DiagnosticCheck[],
  activeTransport: string | null,
  options: {
    fallbackOrder: string[];
    runtimeState?: PluginRuntimeSnapshot;
    ignoredWarnIds?: ReadonlySet<string>;
  },
): DiagnosticReport {
  const enrichedChecks = checks.map(enrichDiagnosticCheck);
  const ignoredWarnIds = options.ignoredWarnIds ?? new Set<string>();
  const status = resolveReportStatus(enrichedChecks, ignoredWarnIds);
  const nextActions = Array.from(
    new Set(
      enrichedChecks
        .filter((entry) => !(entry.status === "warn" && ignoredWarnIds.has(entry.id)))
        .map((entry) => readString(entry.action))
        .filter((entry): entry is string => Boolean(entry)),
    ),
  );
  return {
    command,
    ok: status !== "fail",
    status,
    code: `${normalizeDiagnosticCodePart(command)}_${normalizeDiagnosticCodePart(status)}`,
    summary:
      status === "pass"
        ? `${command} passed with ${enrichedChecks.length} check(s).`
        : status === "warn"
          ? `${command} completed with warnings.`
          : `${command} failed. Review the recommended actions.`,
    connectionModel: "persistent-client",
    configuredTransport: configTransport,
    fallbackOrder: options.fallbackOrder,
    activeTransport,
    checks: enrichedChecks,
    ...(options.runtimeState ? { runtimeState: options.runtimeState } : {}),
    ...(nextActions.length > 0 ? { nextActions } : {}),
  };
}

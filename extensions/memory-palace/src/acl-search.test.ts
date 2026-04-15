import { describe, expect, test } from "bun:test";
import type { AclSearchDeps } from "./acl-search.ts";
import {
  resolveAclPolicy,
  isUriAllowedByAcl,
  isUriWritableByAcl,
} from "./acl-search.ts";
import type { PluginConfig, ResolvedAclPolicy } from "./types.js";

/* ---------- stub helpers ---------- */

function makeDeps(overrides?: Partial<AclSearchDeps>): AclSearchDeps {
  return {
    appendUriPath: (base, ...segments) =>
      [base, ...segments.filter(Boolean)].join("/").replace(/\/+/g, "/"),
    escapeMemoryForPrompt: (v) => v,
    getParam: (params, name) => params[name],
    normalizeUriPrefix: (prefix, _defaultDomain) => prefix,
    parseJsonRecordWithWarning: () => undefined,
    profileBlockDisclaimer: "",
    profileBlockRootUri: "core://profile",
    profileBlockTag: "memory-palace-profile",
    readBoolean: (v) => (typeof v === "boolean" ? v : undefined),
    readString: (v) => (typeof v === "string" ? v : undefined),
    readStringArray: (v) => (Array.isArray(v) ? v.filter((e): e is string => typeof e === "string") : []),
    renderTemplate: (tpl, r) => tpl.replace(/\{agentId\}/g, r.agentId ?? ""),
    safeSegment: (v) => String(v ?? "").replace(/[^a-zA-Z0-9_-]/g, "_"),
    splitUri: (uri, defaultDomain) => {
      const match = uri.match(/^([^:]+):\/\/(.*)$/);
      return match ? { domain: match[1], path: match[2] } : { domain: defaultDomain, path: uri };
    },
    uriPrefixMatches: (uri, prefix, defaultDomain) => {
      const u = uri.startsWith(`${defaultDomain}://`) ? uri : `${defaultDomain}://${uri}`;
      const p = prefix.startsWith(`${defaultDomain}://`) ? prefix : `${defaultDomain}://${prefix}`;
      return u.startsWith(p);
    },
    ...overrides,
  };
}

function makeConfig(overrides?: Partial<PluginConfig>): PluginConfig {
  return {
    acl: {
      enabled: false,
      agents: {},
      sharedUriPrefixes: ["core://shared"],
      sharedWriteUriPrefixes: [],
      defaultPrivateRootTemplate: "core://private/{agentId}",
      defaultDisclosure: "silent",
      allowIncludeAncestors: true,
    },
    mapping: { defaultDomain: "core" },
    reflection: { rootUri: "core://reflection" },
    query: { filters: {} },
    ...overrides,
  } as unknown as PluginConfig;
}

/* ---------- resolveAclPolicy ---------- */

describe("resolveAclPolicy", () => {
  test("returns disabled policy when ACL is off", () => {
    const config = makeConfig();
    const policy = resolveAclPolicy(config, "agent-a", makeDeps());
    expect(policy.enabled).toBe(false);
    expect(policy.agentId).toBe("agent-a");
  });

  test("returns enabled policy with allowed prefixes when ACL is on", () => {
    const config = makeConfig({
      acl: {
        enabled: true,
        agents: {},
        sharedUriPrefixes: ["core://shared"],
        sharedWriteUriPrefixes: [],
        defaultPrivateRootTemplate: "core://private/{agentId}",
        defaultDisclosure: "silent",
        allowIncludeAncestors: true,
      },
    } as Partial<PluginConfig>);
    const policy = resolveAclPolicy(config, "agent-b", makeDeps());
    expect(policy.enabled).toBe(true);
    expect(policy.allowedUriPrefixes.length).toBeGreaterThan(0);
  });

  test("returns scoped write roots for agent with ID", () => {
    const config = makeConfig();
    const policy = resolveAclPolicy(config, "myagent", makeDeps());
    expect(policy.writeRoots.length).toBeGreaterThan(0);
  });

  test("returns empty scoped roots when agentId is undefined", () => {
    const config = makeConfig();
    const policy = resolveAclPolicy(config, undefined, makeDeps());
    expect(policy.writeRoots).toEqual([]);
  });

  test("uses agent-specific policy when defined in config.acl.agents", () => {
    const config = makeConfig({
      acl: {
        enabled: true,
        agents: {
          "special-agent": {
            allowedDomains: ["custom"],
            allowedUriPrefixes: ["custom://data"],
            writeRoots: ["custom://data/write"],
          },
        },
        sharedUriPrefixes: [],
        sharedWriteUriPrefixes: [],
        defaultPrivateRootTemplate: "core://private/{agentId}",
        defaultDisclosure: "silent",
        allowIncludeAncestors: true,
      },
    } as Partial<PluginConfig>);
    const policy = resolveAclPolicy(config, "special-agent", makeDeps());
    expect(policy.enabled).toBe(true);
    expect(policy.explicitAllowedDomains).toContain("custom");
  });
});

/* ---------- isUriAllowedByAcl ---------- */

describe("isUriAllowedByAcl", () => {
  test("allows everything when policy is disabled", () => {
    const policy: ResolvedAclPolicy = {
      enabled: false,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: [],
      allowedUriPrefixes: [],
      writeRoots: [],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriAllowedByAcl("anything://path", policy, "core", makeDeps())).toBe(true);
  });

  test("allows URI matching an allowed prefix", () => {
    const policy: ResolvedAclPolicy = {
      enabled: true,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: ["core"],
      allowedUriPrefixes: ["core://shared"],
      writeRoots: [],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriAllowedByAcl("core://shared/doc", policy, "core", makeDeps())).toBe(true);
  });

  test("denies URI not matching any allowed prefix or domain", () => {
    const policy: ResolvedAclPolicy = {
      enabled: true,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: ["core"],
      allowedUriPrefixes: ["core://private/a"],
      writeRoots: [],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriAllowedByAcl("other://secret", policy, "core", makeDeps())).toBe(false);
  });

  test("allows URI when domain is in explicitAllowedDomains", () => {
    const policy: ResolvedAclPolicy = {
      enabled: true,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: ["shared"],
      allowedDomains: ["shared"],
      allowedUriPrefixes: [],
      writeRoots: [],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriAllowedByAcl("shared://anything", policy, "core", makeDeps())).toBe(true);
  });
});

/* ---------- isUriWritableByAcl ---------- */

describe("isUriWritableByAcl", () => {
  test("allows write when policy is disabled", () => {
    const policy: ResolvedAclPolicy = {
      enabled: false,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: [],
      allowedUriPrefixes: [],
      writeRoots: [],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriWritableByAcl("any://path", policy, "core", makeDeps())).toBe(true);
  });

  test("allows write when URI matches a writeRoot", () => {
    const policy: ResolvedAclPolicy = {
      enabled: true,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: ["core"],
      allowedUriPrefixes: [],
      writeRoots: ["core://private/a"],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriWritableByAcl("core://private/a/doc", policy, "core", makeDeps())).toBe(true);
  });

  test("denies write when URI does not match any writeRoot", () => {
    const policy: ResolvedAclPolicy = {
      enabled: true,
      agentId: "a",
      agentKey: "a",
      explicitAllowedDomains: [],
      allowedDomains: ["core"],
      allowedUriPrefixes: [],
      writeRoots: ["core://private/a"],
      allowIncludeAncestors: true,
      disclosure: "silent",
    };
    expect(isUriWritableByAcl("core://private/b/doc", policy, "core", makeDeps())).toBe(false);
  });
});

/** Server-only API setup bridge; never send the setup capability to a browser. */
import { potbotConfigForRequest, upstream } from "./potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "./security.ts";

const PROVIDERS = new Set(["openai", "anthropic", "gemini", "xai", "evolink"]);
const STATES = new Set(["configured", "missing", "invalid_environment", "invalid_vault", "vault_unavailable"]);
const SOURCES = new Set(["native_vault", "environment", null]);
const RESULTS = new Set(["reachable", "not_configured", "authentication_rejected", "rate_limited", "provider_unavailable"]);

export type ProviderSetup = {
  id: string; label: string; configured: boolean; status: string;
  source: "native_vault" | "environment" | null; editable: boolean; environmentVariable: string;
};
export type ApiSetup = { schema: "potbot-api-setup/v1"; workspaceId: string;
  scope: "operator_credential_profile"; storage: "native_os_credential_vault";
  profile: string; secretsExposed: false; providers: ProviderSetup[] };

const ENV: Record<string, string> = {
  openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY",
  gemini: "GEMINI_API_KEY", xai: "XAI_API_KEY", evolink: "EVOLINK_API_KEY",
};

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

/** Pure, allowlisted schema projection. Discard all unknown fields. */
export function projectSetupProvider(value: unknown): ProviderSetup | null {
  const p = object(value);
  if (!p || typeof p.id !== "string" || !PROVIDERS.has(p.id) ||
      typeof p.label !== "string" || p.label.length < 1 || p.label.length > 100 ||
      typeof p.configured !== "boolean" || typeof p.editable !== "boolean" ||
      !STATES.has(p.status as string) || !SOURCES.has(p.source as null) ||
      p.environmentVariable !== ENV[p.id]) return null;
  return {
    id: p.id, label: p.label, configured: p.configured,
    status: p.status as string, source: p.source as ProviderSetup["source"],
    editable: p.editable, environmentVariable: p.environmentVariable as string,
  };
}
export function projectApiSetup(value: unknown, workspaceId: string): ApiSetup | null {
  const s = object(value);
  if (!s || s.schema !== "potbot-api-setup/v1" ||
      s.scope !== "operator_credential_profile" || s.storage !== "native_os_credential_vault" ||
      s.secretsExposed !== false || typeof s.profile !== "string" || s.profile.length > 64 ||
      !Array.isArray(s.providers) || s.providers.length !== 5) return null;
  const providers = s.providers.map(projectSetupProvider);
  if (providers.some(p => p === null) || new Set(providers.map(p => p?.id)).size !== 5)
    return null;
  return { schema: "potbot-api-setup/v1", workspaceId,
    scope: "operator_credential_profile", storage: "native_os_credential_vault",
    profile: s.profile, secretsExposed: false, providers: providers as ProviderSetup[] };
}

export function setupCapability(env: NodeJS.ProcessEnv = process.env): string | null {
  const key = env.POTBOT_UI_SETUP_TOKEN;
  return typeof key === "string" && key.length >= 32 && key.length <= 4096 &&
      /^[!-~]+$/.test(key) ? key : null;
}

async function boundedResponse(upstreamResponse: Response): Promise<unknown> {
  const bytes = await upstreamResponse.arrayBuffer();
  if (bytes.byteLength > 16384) throw new Error("API setup status exceeded limit");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

/** A single trusted entry for GET/PUT/DELETE/POST, no reflected upstream body. */
export async function apiSetupRequest(
  request: Request, method: "GET" | "PUT" | "DELETE" | "POST",
  provider?: string,
): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (method !== "GET" && !validOrigin(request, operator))
    return safeError("Untrusted request origin.", 403);
  if (new URL(request.url).search)
    return safeError("Unexpected API setup query.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend not configured.", 503);
  const setup = setupCapability();
  if (!setup) return safeError("Operator API setup has not been provisioned.", 503);
  if (provider !== undefined && !PROVIDERS.has(provider))
    return safeError("Unknown API provider.", 400);
  const path = "/v1/ui/setup/apis" + (provider ? `/${provider}` : "") + (method === "POST" ? "/test" : "");
  let body: string | undefined;
  if (method !== "GET") {
    let raw: unknown;
    try { raw = await boundedJSON(request, 6144); }
    catch { return safeError("Invalid credential setup request.", 400); }
    const item = object(raw);
    if (!item) return safeError("Invalid API setup request.", 400);
    if (method === "PUT") {
      if (Object.keys(item).sort().join(",") !== "confirm,key,replace" ||
          item.confirm !== true || typeof item.replace !== "boolean" ||
          typeof item.key !== "string" || item.key.length < 1 || item.key.length > 4096 ||
          !/^[!-~]+$/.test(item.key))
        return safeError("Valid API key and explicit confirmation required.", 400);
      body = JSON.stringify({ key: item.key, confirm: true, replace: item.replace });
    } else if (Object.keys(item).join(",") !== "confirm" || item.confirm !== true) {
      return safeError("Explicit provider operation confirmation required.", 400);
    } else body = '{"confirm":true}';
  }
  try {
    const result = await upstream(config, path, {
      method, headers: { "Origin": operator.publicOrigin,
        "X-Potbot-Setup-Token": setup,
        ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body } : {}),
    }, method === "POST" ? 65000 : 12000);
    // Never forward an upstream error body: vault and provider libraries may
    // unexpectedly include secret-bearing transport diagnostics.
    if (!result.ok) {
      const status = result.status === 409 ? 409 : result.status === 503 ? 503 : 502;
      return safeError(status === 409 ? "API credential changed; reload status." :
        "Operator API setup request failed.", status);
    }
    const document = await boundedResponse(result);
    if (method === "GET") {
      const projected = projectApiSetup(document, config.workspaceId);
      return projected ? Response.json(projected, { headers: { "Cache-Control": "private, no-store" } }) :
        safeError("Invalid credential inventory.", 502);
    }
    if (method === "POST") {
      const record = object(document);
      if (!record || record.id !== provider || typeof record.verified !== "boolean" ||
          typeof record.status !== "string" || !RESULTS.has(record.status) ||
          !(record.modelCount === undefined ||
            (typeof record.modelCount === "number" && Number.isSafeInteger(record.modelCount) &&
            record.modelCount >= 0 && record.modelCount <= 10000)))
        return safeError("Invalid provider test result.", 502);
      return Response.json({ id: provider, verified: record.verified, status: record.status,
        ...(record.modelCount !== undefined ? { modelCount: record.modelCount } : {}) },
      { headers: { "Cache-Control": "private, no-store" } });
    }
    const item = projectSetupProvider(document);
    if (!item || item.id !== provider) return safeError("Invalid credential operation response.", 502);
    return Response.json(item, { headers: { "Cache-Control": "private, no-store" } });
  } catch {
    return safeError("API credential service unavailable.", 503);
  }
}

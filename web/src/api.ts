import { providerPresets, type ProviderPreset } from "./providers";

export { providerPresets };
export type { ProviderPreset } from "./providers";

export type ConnectionStatus = "connected" | "checking" | "failed" | "unknown";
export type DeliveryStatus =
  | "queued"
  | "sending"
  | "sent"
  | "failed"
  | "ambiguous";
export type SummaryStatus = "pending" | "generating" | "generated" | "failed" | "unknown" | string;
export type LlmSettings = {
  enabled: boolean;
  baseUrl: string;
  model: string;
  defaultLanguage: string;
  /** Whether an API key is stored server-side; the secret is never returned. */
  apiKeyConfigured: boolean;
  summaryThreshold: number;
  lastTestAt?: string | null;
  lastTestStatus?: string | null;
  failedCount: number;
  lastError?: string | null;
};

export type Account = {
  id: string;
  email: string;
  name: string;
  provider: string;
  status: ConnectionStatus;
  credentialConfigured: boolean;
  signature?: string | null;
  imapServer?: string;
  imapPort?: number;
  imapSsl?: boolean;
  smtpServer?: string;
  smtpPort?: number;
  smtpSsl?: boolean;
  enabled?: boolean;
  connectionStatus?: string;
  connectionError?: string | null;
  lastVerifiedAt?: number | null;
  nextVerificationAt?: number | null;
};
export type Contact = { email: string; name?: string };
export type Attachment = { id?: string; name: string; size: number; file?: File };
export type InlineAsset = {
  id: string;
  contentId: string;
  mimeType: string;
  size: number;
};
export type Message = {
  id: string;
  accountId?: string;
  from: string;
  to: string[];
  cc?: string[];
  date: string;
  body: string;
  subject?: string;
  html?: string;
  summary?: string | null;
  summaryStatus?: SummaryStatus;
  summaryErrorCode?: string | null;
  priority?: string | null;
  category?: string | null;
  summaryUpdatedAt?: string | null;
  inlineAssets?: InlineAsset[];
};
export type Thread = {
  id: string;
  subject: string;
  preview: string;
  participants: string[];
  date: string;
  unread?: boolean;
  accountId: string;
  messages: Message[];
  summary?: string | null;
  summaryStatus?: SummaryStatus;
  summaryErrorCode?: string | null;
  priority?: string | null;
  category?: string | null;
  summaryUpdatedAt?: string | null;
};
export type ComposePayload = {
  accountId: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  attachments: Attachment[];
  replyTo?: string;
  forwardOf?: string;
};
type AuthStatus = {
  authenticated: boolean;
  telegram_user_id?: number;
  csrf_token?: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
type Operation = {
  id: string;
  kind: string;
  status:
    | DeliveryStatus
    | "accepted"
    | "running"
    | "succeeded"
    | "deleted"
    | "deleting";
  error?: string | null;
};
type AccountWire = {
  id?: string | number;
  email: string;
  alias?: string;
  imap_server?: string;
  imap_port?: number;
  imap_ssl?: boolean;
  smtp_server?: string;
  smtp_port?: number;
  smtp_ssl?: boolean;
  signature?: string | null;
  enabled?: boolean;
  credential_configured: boolean;
  connection_status?: string;
  connection_error?: string | null;
  last_verified_at?: number | null;
  next_verification_at?: number | null;
};
type AttachmentWire = {
  id: string | number;
  file_name: string;
  mime_type?: string | null;
  size?: number | null;
  status: "available" | "legacy_missing";
  draft_version: number;
};
type VerifyWire = {
  imap: { ok: boolean; error?: string | null };
  smtp: { ok: boolean; error?: string | null };
};
type ThreadWire = {
  id: string;
  account_id?: number;
  subject?: string;
  latest_at?: string;
  message_count?: number;
  preview?: string | null;
  summary?: string | null;
  summary_status?: SummaryStatus | null;
  summary_error_code?: string | null;
  priority?: string | null;
  category?: string | null;
  summary_updated_at?: string | number | null;
};
type MessageWire = {
  id?: string | number;
  thread_id?: string;
  account_id?: number;
  sender?: string;
  recipient?: string;
  cc?: string;
  subject?: string;
  body_text?: string;
  body_html?: string;
  email_date?: string;
  summary?: string | null;
  summary_status?: SummaryStatus | null;
  summary_error_code?: string | null;
  priority?: string | null;
  category?: string | null;
  summary_updated_at?: string | number | null;
  inline_assets?: Array<{
    id?: string | number;
    content_id?: string;
    mime_type?: string;
    size?: number;
  }>;
};
type LlmSettingsWire = {
  enabled?: boolean;
  base_url?: string;
  model?: string;
  default_language?: string;
  api_key_configured?: boolean;
  summary_threshold?: number;
  last_test_at?: string | null;
  last_tested_at?: string | number | null;
  last_test_status?: string | null;
  failed_count?: number;
  last_error?: string | null;
};

const mockEnabled = import.meta.env.VITE_API_MODE === "mock";
let csrfToken: string | null = null;
let mockAccounts: Account[] = [
  {
    id: "1",
    email: "me@example.com",
    name: "示例账户",
    provider: "custom",
    status: "unknown",
    credentialConfigured: true,
    signature: "Sent from Telegramail",
    imapServer: "imap.example.com",
    imapPort: 993,
    imapSsl: true,
    smtpServer: "smtp.example.com",
    smtpPort: 465,
    smtpSsl: true,
    enabled: false,
  },
];
const mockThreads: Thread[] = [
  {
    id: "1",
    subject: "欢迎使用 Telegramail",
    preview: "这是一个可回复的演示邮件线程。",
    participants: ["Telegramail <hello@telegramail.dev>", "me@example.com"],
    date: new Date().toISOString(),
    unread: true,
    accountId: "1",
    messages: [
      {
        id: "m1",
        from: "Telegramail <hello@telegramail.dev>",
        to: ["me@example.com"],
        date: new Date().toISOString(),
        body: "欢迎使用 Telegramail。\n\n在这里可以阅读、回复和转发邮件。",
        summary: "欢迎使用 <b>Telegramail</b>，可在这里<i>阅读、回复和转发邮件</i>。",
        summaryStatus: "generated",
      },
    ],
    summary: "欢迎使用 <b>Telegramail</b>，可在这里<i>阅读、回复和转发邮件</i>。",
    summaryStatus: "generated",
  },
];
let mockLlmSettings: LlmSettings = {
  enabled: false,
  baseUrl: "https://api.openai.com/v1",
  model: "gpt-4o-mini",
  defaultLanguage: "en_US",
  apiKeyConfigured: false,
  summaryThreshold: 120,
  lastTestAt: null,
  lastTestStatus: null,
  failedCount: 0,
  lastError: null,
};
const wait = (ms = 240) => new Promise((resolve) => setTimeout(resolve, ms));
const splitAddresses = (value?: string | null) =>
  (value ?? "")
    .split(/[;,]/)
    .map((item) => item.trim())
    .filter(Boolean);
const providerFor = (value: {
  imapServer: string; imapPort: number; imapSsl: boolean;
  smtpServer: string; smtpPort: number; smtpSsl: boolean;
}): ProviderPreset =>
  (Object.entries(providerPresets).find(([, preset]) =>
    preset.imapServer === value.imapServer && preset.imapPort === value.imapPort && preset.imapSsl === value.imapSsl &&
    preset.smtpServer === value.smtpServer && preset.smtpPort === value.smtpPort && preset.smtpSsl === value.smtpSsl,
  )?.[0] as ProviderPreset | undefined) ?? "custom";
const toAccount = (value: AccountWire): Account => {
  const transport = {
    imapServer: value.imap_server ?? "",
    imapPort: value.imap_port ?? 993,
    imapSsl: value.imap_ssl ?? true,
    smtpServer: value.smtp_server ?? "",
    smtpPort: value.smtp_port ?? 465,
    smtpSsl: value.smtp_ssl ?? true,
  };
  return {
  id: String(value.id ?? value.email),
  email: value.email,
  name: value.alias || value.email,
  provider: providerFor(transport),
  status: (value.connection_status as ConnectionStatus | undefined) ?? (value.enabled ? "connected" : "unknown"),
  credentialConfigured: value.credential_configured,
  signature: value.signature || undefined,
  enabled: Boolean(value.enabled),
  connectionStatus: value.connection_status ?? (value.enabled ? "connected" : "unknown"),
  connectionError: value.connection_error ?? null,
  lastVerifiedAt: value.last_verified_at ?? null,
  nextVerificationAt: value.next_verification_at ?? null,
  ...transport,
};
};
const toThread = (value: ThreadWire): Thread => ({
  id: value.id,
  accountId: String(value.account_id ?? ""),
  subject: value.subject || "（无主题）",
  preview: value.summary || value.preview || (value.message_count
    ? `${value.message_count} 封邮件`
    : "没有可用的邮件预览"),
  participants: [],
  date: value.latest_at || new Date(0).toISOString(),
  messages: [],
  summary: value.summary ?? null,
  summaryStatus: value.summary_status ?? undefined,
  summaryErrorCode: value.summary_error_code ?? null,
  priority: value.priority ?? null,
  category: value.category ?? null,
  summaryUpdatedAt: value.summary_updated_at == null ? null : String(value.summary_updated_at),
});
const toMessage = (value: MessageWire): Message => ({
  id: String(value.id ?? crypto.randomUUID()),
  accountId: value.account_id == null ? undefined : String(value.account_id),
  from: value.sender || "未知发件人",
  to: splitAddresses(value.recipient),
  cc: splitAddresses(value.cc),
  date: value.email_date || new Date(0).toISOString(),
  body: value.body_text || "",
  subject: value.subject || undefined,
  html: value.body_html || undefined,
  summary: value.summary ?? null,
  summaryStatus: value.summary_status ?? undefined,
  summaryErrorCode: value.summary_error_code ?? null,
  priority: value.priority ?? null,
  category: value.category ?? null,
  summaryUpdatedAt: value.summary_updated_at == null ? null : String(value.summary_updated_at),
  inlineAssets: (value.inline_assets ?? [])
    .filter((asset) => asset.id != null && Boolean(asset.content_id))
    .map((asset) => ({
      id: String(asset.id),
      contentId: String(asset.content_id),
      mimeType: String(asset.mime_type ?? ""),
      size: Number(asset.size ?? 0),
    })),
});

function idempotencyKey() {
  return (
    crypto.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}
function rememberSession(status: AuthStatus) {
  csrfToken = status.authenticated ? (status.csrf_token ?? null) : null;
  return status;
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = new Headers(init?.headers);
  if (
    method !== "GET" &&
    method !== "HEAD" &&
    method !== "OPTIONS" &&
    path !== "/auth/setup" &&
    path !== "/auth/session"
  ) {
    if (!csrfToken)
      throw new Error("会话已过期，请重新绑定 Telegram Mini App。");
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok)
    throw new ApiError(
      (await response.json().catch(() => ({}))).detail ??
        `请求失败（${response.status}）`,
      response.status,
    );
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}

export const api = {
  mockEnabled,
  idempotencyKey,
  async authStatus() {
    if (mockEnabled)
      return rememberSession({
        authenticated: true,
        telegram_user_id: 1,
        csrf_token: "mock-csrf",
      });
    return rememberSession(await request<AuthStatus>("/auth/status"));
  },
  async session(initData: string) {
    if (mockEnabled) {
      await wait();
      return rememberSession({
        authenticated: true,
        telegram_user_id: 1,
        csrf_token: "mock-csrf",
      });
    }
    if (!initData)
      throw new Error("请从 Telegram Mini App 中打开此页面以恢复会话。");
    return rememberSession(
      await request<AuthStatus>("/auth/session", {
        method: "POST",
        body: JSON.stringify({ initData }),
      }),
    );
  },
  async setup(code: string, initData: string) {
    if (mockEnabled) {
      await wait();
      if (code.length < 4) throw new Error("绑定码至少需要 4 位");
      return rememberSession({
        authenticated: true,
        telegram_user_id: 1,
        csrf_token: "mock-csrf",
      });
    }
    if (!initData)
      throw new Error("请从 Telegram Mini App 中打开此页面以完成安全绑定。");
    return rememberSession(
      await request<AuthStatus>("/auth/setup", {
        method: "POST",
        body: JSON.stringify({ initData, code }),
      }),
    );
  },
  async accounts(signal?: AbortSignal) {
    return mockEnabled
      ? (await wait(), mockAccounts)
      : (await request<AccountWire[]>("/accounts", { signal })).map(toAccount);
  },
  async llmSettings(signal?: AbortSignal): Promise<LlmSettings> {
    if (mockEnabled) {
      await wait(80);
      return { ...mockLlmSettings };
    }
    const value = await request<{
      enabled?: boolean;
      base_url?: string;
      model?: string;
      default_language?: string;
      api_key_configured?: boolean;
      summary_threshold?: number;
      last_test_at?: string | null;
      last_tested_at?: string | number | null;
      last_test_status?: string | null;
      failed_count?: number;
      last_error?: string | null;
    }>("/settings/llm", { signal });
    return {
      enabled: Boolean(value.enabled),
      baseUrl: value.base_url ?? "",
      model: value.model ?? "",
      apiKeyConfigured: Boolean(value.api_key_configured),
      summaryThreshold: Number(value.summary_threshold ?? 120),
      defaultLanguage: value.default_language ?? "en_US",
      lastTestAt: value.last_test_at ?? (value.last_tested_at == null ? null : String(value.last_tested_at)),
      lastTestStatus: value.last_test_status ?? null,
      failedCount: Number(value.failed_count ?? 0),
      lastError: value.last_error ?? null,
    };
  },
  async saveLlmSettings(settings: {
    enabled: boolean;
    baseUrl: string;
    model: string;
    defaultLanguage: string;
    summaryThreshold: number;
    apiKey?: string;
  }): Promise<LlmSettings> {
    if (mockEnabled) {
      await wait(160);
      mockLlmSettings = {
        ...mockLlmSettings,
        enabled: settings.enabled,
        baseUrl: settings.baseUrl,
        model: settings.model,
        defaultLanguage: settings.defaultLanguage,
        summaryThreshold: settings.summaryThreshold,
        apiKeyConfigured: Boolean(settings.apiKey) || mockLlmSettings.apiKeyConfigured,
      };
      return { ...mockLlmSettings };
    }
    const value = await request<LlmSettingsWire>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify({
        enabled: settings.enabled,
        base_url: settings.baseUrl,
        model: settings.model,
        default_language: settings.defaultLanguage,
        summary_threshold: settings.summaryThreshold,
        ...(settings.apiKey ? { api_key: settings.apiKey } : {}),
      }),
    });
    return {
      enabled: Boolean(value.enabled), baseUrl: value.base_url ?? "", model: value.model ?? "", defaultLanguage: value.default_language ?? "en_US",
      apiKeyConfigured: Boolean(value.api_key_configured), summaryThreshold: Number(value.summary_threshold ?? 120),
      lastTestAt: value.last_test_at ?? (value.last_tested_at == null ? null : String(value.last_tested_at)), lastTestStatus: value.last_test_status ?? null,
      failedCount: Number(value.failed_count ?? 0), lastError: value.last_error ?? null,
    };
  },
  async testLlmConnection(settings?: {
    enabled?: boolean; baseUrl?: string; model?: string; summaryThreshold?: number; apiKey?: string;
  }) {
    if (mockEnabled) {
      await wait(350);
      const ok = Boolean(settings?.baseUrl ?? mockLlmSettings.baseUrl) && Boolean(settings?.model ?? mockLlmSettings.model);
      mockLlmSettings = { ...mockLlmSettings, lastTestAt: new Date().toISOString(), lastTestStatus: ok ? "ok" : "failed", lastError: ok ? null : "请填写 Base URL 与模型" };
      return { ok, status: ok ? "ok" : "failed", error: mockLlmSettings.lastError };
    }
    return request<{ ok: boolean; status?: string; last_test_status?: string; error?: string | null }>("/settings/llm/test", {
      method: "POST",
      body: JSON.stringify({
        enabled: settings?.enabled,
        base_url: settings?.baseUrl,
        model: settings?.model,
        summary_threshold: settings?.summaryThreshold,
        ...(settings?.apiKey ? { api_key: settings.apiKey } : {}),
      }),
    });
  },
  async retrySummary(emailId: string) {
    if (mockEnabled) {
      await wait(180);
      const thread = mockThreads.find((item) => item.id === emailId) || mockThreads.find((item) => item.messages.some((message) => message.id === emailId));
      if (thread) {
        thread.summaryStatus = "generating";
        thread.messages.forEach((message) => { message.summaryStatus = "generating"; });
        setTimeout(() => {
          thread.summaryStatus = "generated";
          thread.summary = thread.summary || thread.messages.at(-1)?.body.slice(0, 120) || "";
          thread.messages.forEach((message) => {
            message.summaryStatus = "generated";
            message.summary = message.summary || message.body.slice(0, 120);
          });
        }, 650);
      }
      return { status: "generating" as SummaryStatus };
    }
    return request<{ status: SummaryStatus }>(`/emails/${encodeURIComponent(emailId)}/summary/retry`, { method: "POST" });
  },
  async saveAccount(account: Partial<Account> & { password?: string; signature?: string | null }) {
    if (mockEnabled) {
      await wait();
      const saved: Account = {
        id: account.id ?? String(mockAccounts.length + 1),
        email: account.email ?? "",
        name: account.name ?? "",
        provider: account.provider ?? "custom",
        status: "unknown",
        credentialConfigured:
          Boolean(account.password) || Boolean(account.credentialConfigured),
        signature: account.signature,
        imapServer: account.imapServer ?? "",
        imapPort: account.imapPort ?? 993,
        imapSsl: account.imapSsl ?? true,
        smtpServer: account.smtpServer ?? "",
        smtpPort: account.smtpPort ?? 465,
        smtpSsl: account.smtpSsl ?? true,
        enabled: Boolean(account.enabled),
      };
      mockAccounts = account.id
        ? mockAccounts.map((item) =>
            item.id === account.id ? { ...item, ...saved } : item,
          )
        : [...mockAccounts, saved];
      return saved;
    }
    const payload = {
      email: account.email,
      alias: account.name,
      password: account.password,
      signature: account.signature,
      imap_server: account.imapServer,
      imap_port: account.imapPort,
      imap_ssl: account.imapSsl,
      smtp_server: account.smtpServer,
      smtp_port: account.smtpPort,
      smtp_ssl: account.smtpSsl,
    };
    const wire = account.id
      ? await request<AccountWire>(
          `/accounts/${encodeURIComponent(account.id)}`,
          { method: "PATCH", body: JSON.stringify(payload) },
        )
      : await request<AccountWire>("/accounts", {
          method: "POST",
          body: JSON.stringify(payload),
        });
    return toAccount(wire);
  },
  async removeAccount(id: string, purgeData = false) {
    if (mockEnabled) {
      await wait();
      if (purgeData) mockAccounts = mockAccounts.filter((item) => item.id !== id);
      else mockAccounts = mockAccounts.filter((item) => item.id !== id);
      return { id, status: "deleted", purgeData };
    }
    return request<{ id: string | number; status: string; purge_data?: boolean; error?: string | null }>(
      `/accounts/${encodeURIComponent(id)}`,
      { method: "DELETE", body: JSON.stringify({ purge_data: purgeData }) },
    );
  },
  async verifyAccount(id: string) {
    if (mockEnabled) {
      await wait(350);
      return { imap: { ok: true }, smtp: { ok: true } } satisfies VerifyWire;
    }
    return request<VerifyWire>(`/accounts/${encodeURIComponent(id)}/verify`, {
      method: "POST",
    });
  },
  async contacts(query: string, accountId?: string) {
    if (mockEnabled) {
      await wait(80);
      return [
        { name: "Alice", email: "alice@example.com" },
        { name: "Support", email: "support@example.com" },
      ].filter((contact) =>
        `${contact.name} ${contact.email}`
          .toLowerCase()
          .includes(query.toLowerCase()),
      );
    }
    const scope = accountId
      ? `&account_id=${encodeURIComponent(accountId)}`
      : "";
    return request<Contact[]>(
      `/contacts?q=${encodeURIComponent(query)}${scope}`,
    );
  },
  async threads(signal?: AbortSignal) {
    return mockEnabled
      ? (await wait(), mockThreads)
      : (await request<ThreadWire[]>("/threads", { signal })).map(toThread);
  },
  async thread(id: string, signal?: AbortSignal) {
    if (mockEnabled) {
      await wait();
      return mockThreads.find((thread) => thread.id === id)!;
    }
    const messages = (
      await request<MessageWire[]>(
        `/threads/${encodeURIComponent(id)}/messages`,
        { signal },
      )
    ).map(toMessage);
    const last = messages.at(-1);
    return {
      id,
      accountId: last?.accountId ?? "",
      subject: last?.subject || "（无主题）",
      participants: [
        ...new Set(
          messages.flatMap((message) => [message.from, ...message.to]),
        ),
      ],
      date: last?.date ?? new Date(0).toISOString(),
      messages,
      preview: last?.summary || last?.body.slice(0, 120) || "",
      summary: last?.summary ?? null,
      summaryStatus: last?.summaryStatus,
      summaryErrorCode: last?.summaryErrorCode ?? null,
      priority: last?.priority ?? null,
      category: last?.category ?? null,
      summaryUpdatedAt: last?.summaryUpdatedAt ?? null,
    };
  },
  async send(payload: ComposePayload, key: string) {
    if (mockEnabled) {
      await wait(650);
      if (payload.subject.toLowerCase().includes("fail"))
        throw new Error("模拟发送失败");
      return { status: "sent" as DeliveryStatus, id: idempotencyKey() };
    }
    const draft = await request<{ id: string | number; version: number }>("/drafts", {
      method: "POST",
      body: JSON.stringify({
        account_id: Number(payload.accountId),
        draft_type: payload.replyTo
          ? "reply"
          : payload.forwardOf
            ? "forward"
            : "compose",
        to_addrs: payload.to.join(", "),
        cc_addrs: payload.cc.join(", "),
        bcc_addrs: payload.bcc.join(", "),
        subject: payload.subject,
        body_markdown: payload.body,
      }),
    });
    let draftVersion = draft.version;
    for (const attachment of payload.attachments) {
      if (!attachment.file) continue;
      const form = new FormData();
      form.append("file", attachment.file, attachment.name);
      const saved = await request<AttachmentWire>(
        `/drafts/${encodeURIComponent(String(draft.id))}/attachments`,
        {
          method: "POST",
          headers: { "If-Match": `"${draftVersion}"` },
          body: form,
        },
      );
      draftVersion = saved.draft_version;
    }
    const operation = await request<Operation>(
      `/drafts/${encodeURIComponent(String(draft.id))}/send`,
      { method: "POST", headers: { "Idempotency-Key": key } },
    );
    return {
      status:
        operation.status === "accepted" || operation.status === "running"
          ? ("queued" as DeliveryStatus)
          : (operation.status as DeliveryStatus),
      id: operation.id,
    };
  },
  async removeThread(id: string, key: string) {
    if (mockEnabled) {
      await wait();
      const index = mockThreads.findIndex((thread) => thread.id === id);
      if (index >= 0) mockThreads.splice(index, 1);
      return { id: key, status: "deleted" as const };
    }
    return request<Operation>(`/threads/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": key },
    });
  },
};

import {
  StrictMode,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import MarkdownIt from "markdown-it";
import {
  ApiError,
  api,
  type Account,
  type Attachment,
  type ComposePayload,
  type Contact,
  type DeliveryStatus,
  type LlmSettings,
  type SummaryStatus,
  type Thread,
} from "./api";
import { providerPresets, type ProviderPreset } from "./providers";
import "./styles.css";

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData?: string;
        ready(): void;
        expand(): void;
        colorScheme?: "light" | "dark";
        themeParams?: Record<string, string>;
        onEvent(event: string, cb: () => void): void;
      };
    };
  }
}

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// Keep the browser renderer aligned with the Python renderer used by the send
// worker.  The default preset gives users the Markdown features they expect
// (headings, lists, tables, strikethrough, links) while `html: false` keeps
// pasted markup inert instead of allowing it to become email HTML.
const markdownRenderer = new MarkdownIt("default", { html: false, breaks: true });

const emailHtmlTags = new Set([
  "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2",
  "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
  "s", "span", "strong", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
]);
const emailHtmlAttributes = new Set([
  "alt", "colspan", "data-telegramail-external-src", "height", "href", "rowspan", "src", "style", "target", "title", "width",
]);
const emailImageSourceAttributes = ["src", "srcset", "data-src", "data-original", "data-lazy-src", "data-image-src"];
const emailHtmlRemovedTags = new Set(["base", "embed", "form", "iframe", "link", "meta", "object", "script", "style"]);
const summaryHtmlTags = new Set(["b", "i", "code", "br"]);

/** Sanitize provider HTML before putting it in the DOM. Email is untrusted input. */
export function sanitizeEmailHtml(
  raw: string,
  options: { allowExternalImages?: boolean; inlineImageUrls?: Record<string, string> } | boolean = {},
) {
  if (!raw.trim() || typeof DOMParser === "undefined") return "";
  const allowExternalImages = typeof options === "boolean"
    ? options
    : options.allowExternalImages === true;
  const inlineImageUrls = typeof options === "boolean" ? {} : options.inlineImageUrls ?? {};
  const inlineImageLookup = new Map<string, string>();
  for (const [contentId, url] of Object.entries(inlineImageUrls)) {
    const normalizedId = normalizeInlineContentId(contentId);
    const normalizedUrl = normalizeInlineImageUrl(url);
    if (normalizedId && normalizedUrl) inlineImageLookup.set(normalizedId.toLowerCase(), normalizedUrl);
  }
  const parsed = new DOMParser().parseFromString(`<div>${raw}</div>`, "text/html");
  const root = parsed.body.firstElementChild;
  if (!root) return "";
  const clean = (node: Element) => {
    for (const child of Array.from(node.children)) {
      const tag = child.tagName.toLowerCase();
      if (emailHtmlRemovedTags.has(tag)) {
        child.remove();
        continue;
      }
      const inlineSource = tag === "img" ? inlineImageSource(child, inlineImageLookup) : null;
      const imageSource = tag === "img" ? externalImageSource(child) : null;
      for (const attribute of Array.from(child.attributes)) {
        const name = attribute.name.toLowerCase();
        if (name === "data-telegramail-external-src") {
          // This marker is generated only after normalizing a safe HTTPS src; a
          // provider-supplied marker must never be trusted as an image source.
          child.removeAttribute(attribute.name);
          continue;
        }
        if (!emailHtmlAttributes.has(name)) {
          child.removeAttribute(attribute.name);
          continue;
        }
        if (name === "href") {
          try {
            const url = new URL(attribute.value, "https://telegramail.invalid");
            if (!["http:", "https:", "mailto:"].includes(url.protocol) || url.username || url.password) child.removeAttribute(attribute.name);
            else {
              child.setAttribute("href", url.toString());
              child.setAttribute("target", "_blank");
              child.setAttribute("rel", "noopener noreferrer nofollow");
            }
          } catch {
            child.removeAttribute(attribute.name);
          }
        }
        if (name === "style") {
          const safeStyle = sanitizeEmailStyle(attribute.value);
          if (safeStyle) child.setAttribute("style", safeStyle);
          else child.removeAttribute(attribute.name);
        }
        if (name === "src") {
          child.removeAttribute(attribute.name);
        }
      }
      if (tag === "img") {
        for (const attribute of emailImageSourceAttributes) child.removeAttribute(attribute);
        if (inlineSource) {
          child.setAttribute("src", inlineSource);
        } else if (imageSource) {
          if (allowExternalImages) child.setAttribute("src", imageSource);
          else child.setAttribute("data-telegramail-external-src", imageSource);
        }
        child.setAttribute("loading", "lazy");
        child.setAttribute("referrerpolicy", "no-referrer");
      }
      clean(child);
      // Clean descendants before unwrapping an unknown container.  Doing this
      // afterwards would let unsafe attributes inside a custom/SVG wrapper
      // bypass the email sanitizer.
      if (!emailHtmlTags.has(tag)) child.replaceWith(...Array.from(child.childNodes));
    }
  };
  clean(root);
  return root.innerHTML;
}

/** Preserve the one presentation value needed by common branded email headers. */
function sanitizeEmailStyle(value: string) {
  const declarations: string[] = [];
  for (const declaration of value.split(";")) {
    const [rawName, ...rawValue] = declaration.split(":");
    const name = rawName?.trim().toLowerCase();
    const color = rawValue.join(":").trim();
    if ((name === "background" || name === "background-color") && /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(color)) {
      declarations.push(`background-color: ${color}`);
    }
  }
  return declarations.join("; ");
}

function normalizeInlineContentId(value: string | undefined) {
  if (!value) return null;
  let candidate = value.trim();
  if (/^cid:/i.test(candidate)) candidate = candidate.slice(4).trim();
  try {
    candidate = decodeURIComponent(candidate);
  } catch {
    return null;
  }
  if (candidate.startsWith("<") && candidate.endsWith(">")) candidate = candidate.slice(1, -1).trim();
  return candidate && !/[\u0000-\u0020\u007f<>]/.test(candidate) ? candidate : null;
}

function normalizeInlineImageUrl(value: string | undefined) {
  if (!value || !/^\/api\/v1\/emails\/\d+\/inline-assets\/\d+$/.test(value)) return null;
  return value;
}

function inlineImageSource(element: Element, lookup: Map<string, string>) {
  for (const attribute of emailImageSourceAttributes) {
    const value = element.getAttribute(attribute);
    if (!value) continue;
    const candidate = attribute === "srcset"
      ? value.split(",")[0]?.trim().split(/\s+/)[0]
      : value.trim();
    if (!/^cid:/i.test(candidate ?? "")) continue;
    const contentId = normalizeInlineContentId(candidate);
    const resolved = contentId ? lookup.get(contentId.toLowerCase()) : null;
    if (resolved) return resolved;
  }
  return null;
}

function externalImageSource(element: Element) {
  for (const attribute of emailImageSourceAttributes) {
    const value = element.getAttribute(attribute);
    if (!value) continue;
    const candidate = attribute === "srcset"
      ? value.split(",")[0]?.trim().split(/\s+/)[0]
      : value.trim();
    const safe = normalizeExternalImageUrl(candidate);
    if (safe) return safe;
  }
  return null;
}

function normalizeExternalImageUrl(value: string | undefined) {
  if (!value) return null;
  try {
    const url = new URL(value, "https://telegramail.invalid");
    // Upgrade legacy HTTP email pixels before opt-in loading.  This avoids
    // mixed-content blocking while never issuing an unauthenticated HTTP
    // request from the Mini App.
    if (url.protocol === "http:") url.protocol = "https:";
    if (url.protocol !== "https:" || url.username || url.password) return null;
    return url.toString();
  } catch {
    return null;
  }
}

export function hasExternalEmailImages(raw: string) {
  if (!raw.trim() || typeof DOMParser === "undefined") return false;
  const sanitized = sanitizeEmailHtml(raw);
  const parsed = new DOMParser().parseFromString(`<div>${sanitized}</div>`, "text/html");
  return parsed.querySelector("img[data-telegramail-external-src]") !== null;
}

export function renderEmailMarkdown(body: string) {
  return markdownRenderer.render(body || "");
}
/** Render the small, trusted-by-contract HTML subset returned by the LLM. */
export function sanitizeSummaryHtml(raw: string) {
  if (!raw.trim() || typeof DOMParser === "undefined") return "";
  const parsed = new DOMParser().parseFromString(`<div>${raw}</div>`, "text/html");
  const root = parsed.body.firstElementChild;
  if (!root) return "";
  const clean = (node: Element) => {
    for (const child of Array.from(node.children)) {
      const tag = child.tagName.toLowerCase();
      if (!summaryHtmlTags.has(tag)) {
        child.replaceWith(...Array.from(child.childNodes));
        continue;
      }
      for (const attribute of Array.from(child.attributes)) child.removeAttribute(attribute.name);
      clean(child);
    }
  };
  clean(root);
  return root.innerHTML;
}
export function summaryPlainText(raw: string) {
  const sanitized = sanitizeSummaryHtml(raw);
  if (!sanitized || typeof DOMParser === "undefined") return raw;
  return new DOMParser().parseFromString(`<div>${sanitized}</div>`, "text/html").body.textContent?.trim() ?? "";
}
export function getTelegramInitData() {
  const sdkValue = window.Telegram?.WebApp?.initData;
  if (sdkValue) return sdkValue;
  // Telegram Web passes launch data in the fragment before its SDK finishes
  // initializing. Keep the same signed payload intact for server validation.
  return new URLSearchParams(window.location.hash.slice(1)).get("tgWebAppData") ?? "";
}
export function getMiniAppRoute() {
  const params = new URLSearchParams(window.location.search);
  return {
    action: params.get("action") ?? "",
    threadId: params.get("thread_id") ?? "",
  };
}
export const COMPOSE_DRAFT_STORAGE_KEY = "telegramail.composeDraft.v1";
export const RECENT_ACCOUNT_STORAGE_KEY = "telegramail.activeAccountId";
export type SavedComposeDraft = {
  activeAccountId: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
};

export function composeDraftStorageKey(telegramUserId: number | null | undefined) {
  return typeof telegramUserId === "number"
    ? `${COMPOSE_DRAFT_STORAGE_KEY}.user.${telegramUserId}`
    : null;
}

export function loadComposeDraft(
  telegramUserId: number | null | undefined,
): SavedComposeDraft | null {
  const key = composeDraftStorageKey(telegramUserId);
  if (!key) return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const draft = JSON.parse(raw) as Partial<SavedComposeDraft>;
    const arrays = [draft.to, draft.cc, draft.bcc];
    if (
      typeof draft.activeAccountId !== "string" ||
      typeof draft.subject !== "string" ||
      typeof draft.body !== "string" ||
      arrays.some((value) => !Array.isArray(value) || value.some((item) => typeof item !== "string"))
    )
      throw new Error("Invalid compose draft");
    return draft as SavedComposeDraft;
  } catch {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Storage can be unavailable in embedded/private contexts; treat as no draft.
    }
    return null;
  }
}

export function saveComposeDraft(
  telegramUserId: number | null | undefined,
  draft: SavedComposeDraft,
) {
  const key = composeDraftStorageKey(telegramUserId);
  if (!key) return false;
  try {
    window.localStorage.setItem(key, JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

export function clearComposeDraft(telegramUserId: number | null | undefined) {
  const key = composeDraftStorageKey(telegramUserId);
  if (!key) return false;
  try {
    window.localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

/** Keep the last successfully used sender isolated between Telegram users. */
export function recentAccountStorageKey(telegramUserId: number | null | undefined) {
  return typeof telegramUserId === "number"
    ? `${RECENT_ACCOUNT_STORAGE_KEY}.user.${telegramUserId}`
    : null;
}

export function loadRecentAccountId(telegramUserId: number | null | undefined) {
  const key = recentAccountStorageKey(telegramUserId);
  if (!key) return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function saveRecentAccountId(
  telegramUserId: number | null | undefined,
  accountId: string,
) {
  const key = recentAccountStorageKey(telegramUserId);
  if (!key) return false;
  try {
    window.localStorage.setItem(key, accountId);
    return true;
  } catch {
    return false;
  }
}

export function clearRecentAccountId(telegramUserId: number | null | undefined) {
  const key = recentAccountStorageKey(telegramUserId);
  if (!key) return false;
  try {
    window.localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

export function accountIsUsable(account: Pick<Account, "enabled" | "status" | "connectionStatus">) {
  return account.enabled !== false
    && account.status !== "failed"
    && account.connectionStatus !== "failed";
}
const fmtDate = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
const initials = (text?: string) => text?.trim().slice(0, 1).toUpperCase() || "?";
function normalizeSummaryStatus(status?: SummaryStatus | null, summary?: string | null) {
  const value = String(status ?? "").trim().toLowerCase();
  if (["generated", "ready", "completed", "succeeded", "success", "done"].includes(value)) return "generated" as const;
  if (["generating", "running", "processing", "retrying", "scheduled", "waiting", "in_progress", "in-progress"].includes(value)) return "generating" as const;
  if (["failed", "error"].includes(value)) return "failed" as const;
  if (["pending", "queued", "waiting", "not_started", "not-started"].includes(value)) return "pending" as const;
  return summary ? "generated" as const : "pending" as const;
}
function summaryStatusLabel(status?: SummaryStatus | null, summary?: string | null) {
  const normalized = normalizeSummaryStatus(status, summary);
  return normalized === "generated" ? "已生成" : normalized === "generating" ? "生成中" : normalized === "failed" ? "生成失败" : "待生成";
}
export function groupThreadsByAccount(threads: Thread[], accounts: Account[]) {
  const accountMap = new Map(accounts.map((account) => [account.id, account]));
  const groups = new Map<string, { account: Account | null; threads: Thread[] }>();
  for (const thread of threads) {
    const accountId = thread.accountId || "unknown";
    const group = groups.get(accountId) ?? { account: accountMap.get(accountId) ?? null, threads: [] };
    group.threads.push(thread);
    groups.set(accountId, group);
  }
  return Array.from(groups, ([accountId, group]) => ({ accountId, ...group }));
}
export type SignatureItem = {
  id: string;
  name: string;
  markdown: string;
};

export type SignatureSet = {
  items: SignatureItem[];
  defaultId: string | null;
};

const signatureNameFallback = "默认签名";

function uniqueSignatureId(items: SignatureItem[], hint: string) {
  const taken = new Set(items.map((item) => item.id));
  const base = hint.trim() || "signature";
  let candidate = base;
  let suffix = 2;
  while (taken.has(candidate)) candidate = `${base}-${suffix++}`;
  return candidate;
}

export function parseAccountSignatures(raw?: string | null): SignatureSet {
  const text = String(raw ?? "").trim();
  if (!text) return { items: [], defaultId: null };

  try {
    const value = JSON.parse(text) as {
      version?: number;
      default?: string;
      items?: Array<{ id?: string; name?: string; markdown?: string }>;
    };
    if (value.version === 1 && Array.isArray(value.items)) {
      const items: SignatureItem[] = [];
      for (const [index, item] of value.items.entries()) {
        const markdown = typeof item?.markdown === "string" ? item.markdown.trim() : "";
        if (!markdown) continue;
        const id = uniqueSignatureId(items, item?.id?.trim() || `signature-${index + 1}`);
        items.push({
          id,
          name: item?.name?.trim() || signatureNameFallback,
          markdown,
        });
      }
      const defaultId = items.some((item) => item.id === value.default)
        ? value.default!
        : items[0]?.id ?? null;
      return { items, defaultId };
    }
  } catch {
    // Plain text is the v1 storage format and remains a valid signature.
  }

  return {
    items: [{ id: "legacy", name: signatureNameFallback, markdown: text }],
    defaultId: "legacy",
  };
}

export function serializeAccountSignatures(signatures: SignatureSet): string | null {
  const items = signatures.items
    .map((item, index) => ({
      id: item.id.trim() || `signature-${index + 1}`,
      name: item.name.trim() || signatureNameFallback,
      markdown: item.markdown.trim(),
    }))
    .filter((item) => item.markdown);
  if (!items.length) return null;
  const defaultId = items.some((item) => item.id === signatures.defaultId)
    ? signatures.defaultId
    : items[0].id;
  return JSON.stringify({ version: 1, default: defaultId, items });
}

export function selectedAccountSignature(raw?: string | null) {
  const signatures = parseAccountSignatures(raw);
  return signatures.items.find((item) => item.id === signatures.defaultId) ?? signatures.items[0] ?? null;
}

export function resolveAccountSignature(raw?: string | null) {
  return selectedAccountSignature(raw)?.markdown ?? "";
}

export function appendSignature(body: string, signature: string) {
  const text = signature.trim();
  if (!text) return body;
  const current = body.trimEnd();
  return current ? `${current}\n\n${text}` : text;
}

export function removeTrailingSignature(body: string, signature: string) {
  const text = signature.trim();
  if (!text) return body;
  const current = body.trimEnd();
  if (current !== text && !current.endsWith(`\n${text}`)) return body;
  const without = current === text ? "" : current.slice(0, -text.length).trimEnd();
  return without;
}

export function replaceTrailingSignature(body: string, oldSignature: string, newSignature: string) {
  return appendSignature(removeTrailingSignature(body, oldSignature), newSignature);
}
const formatBytes = (size: number) =>
  size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB` : `${Math.ceil(size / 1024)} KiB`;

function useTelegramTheme() {
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    const apply = () => {
      document.documentElement.dataset.theme = app?.colorScheme ?? "light";
      Object.entries(app?.themeParams ?? {}).forEach(([name, value]) =>
        document.documentElement.style.setProperty(
          `--tg-${name.replace(/_/g, "-")}`,
          value,
        ),
      );
    };
    app?.ready();
    app?.expand();
    apply();
    app?.onEvent("themeChanged", apply);
  }, []);
}
function Toast({
  message,
  onClose,
}: {
  message: string | null;
  onClose(): void;
}) {
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(onClose, 3800);
    return () => clearTimeout(timer);
  }, [message, onClose]);
  return message ? (
    <div className="toast" role="status">
      {message}
      <button aria-label="关闭提示" onClick={onClose}>
        ×
      </button>
    </div>
  ) : null;
}
function Loading() {
  return <div className="loading" aria-label="加载中" />;
}

const focusableSelector = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function useDialog(onClose: () => void) {
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const focusables = () =>
      Array.from(element.querySelectorAll<HTMLElement>(focusableSelector));
    const initial = element.querySelector<HTMLElement>("[data-dialog-initial-focus]") ?? focusables()[0];
    initial?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        const active = document.activeElement;
        if (
          active instanceof HTMLElement &&
          active.getAttribute("role") === "combobox" &&
          active.getAttribute("aria-expanded") === "true"
        ) return;
        event.preventDefault();
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    element.addEventListener("keydown", onKeyDown);
    return () => element.removeEventListener("keydown", onKeyDown);
  }, []);
  return dialog;
}

function useModalBackground(
  appShell: React.RefObject<HTMLDivElement | null>,
  open: boolean,
) {
  const returnFocus = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const shell = appShell.current;
    if (!shell) return;
    if (open) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      shell.setAttribute("inert", "");
      shell.setAttribute("aria-hidden", "true");
      return;
    }
    shell.removeAttribute("inert");
    shell.removeAttribute("aria-hidden");
    returnFocus.current?.focus();
    returnFocus.current = null;
  }, [appShell, open]);
}

export function TokenInput({
  label,
  values,
  onChange,
  disabled = false,
}: {
  label: string;
  values: string[];
  onChange(values: string[]): void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [matches, setMatches] = useState<Contact[]>([]);
  const [selected, setSelected] = useState(-1);
  const [error, setError] = useState("");
  const timer = useRef<number | undefined>(undefined);
  const input = useRef<HTMLInputElement>(null);
  const inputId = `recipient-${label}`;
  const listboxId = `${inputId}-suggestions`;
  const add = (candidate: string) => {
    const email = candidate.match(/<([^>]+)>/)?.[1] ?? candidate.trim();
    if (!email) return;
    if (!emailPattern.test(email)) {
      setError(`“${email}” 不是有效邮箱地址`);
      return;
    }
    if (!values.includes(email.toLowerCase()))
      onChange([...values, email.toLowerCase()]);
    setDraft("");
    setMatches([]);
    setSelected(-1);
    setError("");
  };
  const change = (value: string) => {
    setDraft(value);
    setSelected(-1);
    window.clearTimeout(timer.current);
    if (value.trim().length >= 2)
      timer.current = window.setTimeout(
        () =>
          api
            .contacts(value)
            .then(setMatches)
            .catch(() => setMatches([])),
        150,
      );
    else setMatches([]);
  };
  const paste = (event: React.ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData("text");
    if (!/[;,\s]/.test(text)) return;
    event.preventDefault();
    text
      .split(/[;,\s]+/)
      .filter(Boolean)
      .forEach(add);
  };
  const keyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((i) => Math.min(i + 1, matches.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (["Enter", "Tab", ","].includes(event.key)) {
      if (!draft) return;
      event.preventDefault();
      add(matches[selected]?.email ?? draft);
    } else if (event.key === "Escape") {
      if (matches.length) {
        event.preventDefault();
        event.stopPropagation();
        event.nativeEvent.stopImmediatePropagation();
      }
      setMatches([]);
      setSelected(-1);
    } else if (event.key === "Backspace" && !draft && values.length)
      onChange(values.slice(0, -1));
  };
  return (
    <div className="field token-field">
      <label htmlFor={inputId}>{label}</label>
      <div className="tokens" onClick={() => input.current?.focus()}>
        {values.map((email) => (
          <span className="token" key={email}>
            {email}
            <button
              type="button"
              aria-label={`移除 ${email}`}
              onClick={() =>
                onChange(values.filter((value) => value !== email))
              }
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={input}
          value={draft}
          disabled={disabled}
          onChange={(e) => change(e.target.value)}
          onKeyDown={keyDown}
          onPaste={paste}
          id={inputId}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={matches.length > 0}
          aria-controls={matches.length ? listboxId : undefined}
          aria-activedescendant={selected >= 0 ? `${listboxId}-${selected}` : undefined}
          aria-describedby={error ? `${label}-error` : undefined}
          data-dialog-initial-focus={label === "收件人" || undefined}
        />
      </div>
      {error && (
        <p id={`${label}-error`} className="inline-error">
          {error}
        </p>
      )}
      {matches.length > 0 && (
        <ul id={listboxId} className="suggestions" role="listbox" aria-label="联系人建议">
          {matches.map((contact, index) => (
            <li
              id={`${listboxId}-${index}`}
              key={contact.email}
              role="option"
              aria-selected={index === selected}
              onMouseDown={(e) => {
                e.preventDefault();
                add(contact.email);
              }}
            >
              <strong>{contact.name}</strong> {contact.email}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function AccountForm({
  account,
  onSave,
  onClose,
  toast,
  onRequestDelete,
}: {
  account?: Account;
  onSave(account: Account): void;
  onClose(): void;
  toast(message: string): void;
  onRequestDelete?(account: Account): void;
}) {
  const dialog = useDialog(onClose);
  const initialSignatures = parseAccountSignatures(account?.signature);
  const [form, setForm] = useState({
    name: account?.name ?? "",
    email: account?.email ?? "",
    provider: (account?.provider ?? "gmail") as ProviderPreset,
    password: "",
    imapServer: account?.imapServer ?? providerPresets.gmail.imapServer,
    imapPort: account?.imapPort ?? providerPresets.gmail.imapPort,
    imapSsl: account?.imapSsl ?? providerPresets.gmail.imapSsl,
    smtpServer: account?.smtpServer ?? providerPresets.gmail.smtpServer,
    smtpPort: account?.smtpPort ?? providerPresets.gmail.smtpPort,
    smtpSsl: account?.smtpSsl ?? providerPresets.gmail.smtpSsl,
  });
  const [signatures, setSignatures] = useState<SignatureItem[]>(initialSignatures.items);
  const [defaultSignatureId, setDefaultSignatureId] = useState<string | null>(initialSignatures.defaultId);
  const [newSignatureName, setNewSignatureName] = useState("");
  const [newSignatureMarkdown, setNewSignatureMarkdown] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!emailPattern.test(form.email)) return setError("请输入有效邮箱地址");
    if (!account && !form.password)
      return setError("首次添加账户时必须设置密码");
    if (!form.imapServer.trim() || !form.smtpServer.trim())
      return setError("请填写 IMAP 与 SMTP 服务器地址");
    if (![form.imapPort, form.smtpPort].every((port) => Number.isInteger(port) && port >= 1 && port <= 65535))
      return setError("端口必须是 1 至 65535 之间的整数");
    setBusy(true);
    setError("");
    try {
      const saved = await api.saveAccount({
        ...form,
        signature: serializeAccountSignatures({ items: signatures, defaultId: defaultSignatureId }),
        id: account?.id,
        password: form.password || undefined,
      });
      onSave(saved);
      toast("账户已保存。密码不会回显或保留在此界面。");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };
  const addSignature = () => {
    const markdown = newSignatureMarkdown.trim();
    if (!markdown) return;
    const item: SignatureItem = {
      id: uniqueSignatureId(signatures, `signature-${signatures.length + 1}`),
      name: newSignatureName.trim() || signatureNameFallback,
      markdown,
    };
    setSignatures((items) => [...items, item]);
    setDefaultSignatureId((current) => current ?? item.id);
    setNewSignatureName("");
    setNewSignatureMarkdown("");
  };
  const removeSignature = (id: string) => {
    setSignatures((items) => {
      const next = items.filter((item) => item.id !== id);
      setDefaultSignatureId((current) => current === id ? next[0]?.id ?? null : current);
      return next;
    });
  };
  const updateSignature = (id: string, updates: Partial<SignatureItem>) => {
    setSignatures((items) => items.map((item) => item.id === id ? { ...item, ...updates } : item));
  };
  return (
    <section
      ref={dialog}
      className="sheet"
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-title"
    >
      <header>
        <h2 id="account-title">{account ? "编辑账户" : "添加账户"}</h2>
        <button className="icon-button" onClick={onClose} aria-label="关闭">
          ×
        </button>
      </header>
      <form onSubmit={submit}>
        <label className="field">
          显示名称
          <input
            data-dialog-initial-focus
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </label>
        <label className="field">
          邮箱
          <input
            required
            type="email"
            autoComplete="email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
        </label>
        <label className="field">
          服务商
          <select
            value={form.provider}
            onChange={(e) => {
              const provider = e.target.value as ProviderPreset;
              const preset = provider === "custom" ? undefined : providerPresets[provider];
              setForm(preset ? {
                ...form, provider,
                imapServer: preset.imapServer, imapPort: preset.imapPort, imapSsl: preset.imapSsl,
                smtpServer: preset.smtpServer, smtpPort: preset.smtpPort, smtpSsl: preset.smtpSsl,
              } : { ...form, provider });
            }}
          >
            {Object.entries(providerPresets).map(([key, preset]) => <option value={key} key={key}>{preset.label}</option>)}
            <option value="custom">自定义 IMAP / SMTP</option>
          </select>
        </label>
        <div className="field">
          <label htmlFor="imap-server">IMAP 服务器</label>
          <input
            id="imap-server"
            required
            autoComplete="off"
            value={form.imapServer}
            onChange={(e) => setForm({ ...form, provider: "custom", imapServer: e.target.value })}
            placeholder="imap.example.com"
          />
        </div>
        <label className="field">
          IMAP 端口
          <input type="number" required min={1} max={65535} value={form.imapPort}
            onChange={(e) => setForm({ ...form, provider: "custom", imapPort: Number(e.target.value) })} />
        </label>
        <label className="field">
          SMTP 服务器
          <input required autoComplete="off" value={form.smtpServer}
            onChange={(e) => setForm({ ...form, provider: "custom", smtpServer: e.target.value })}
            placeholder="smtp.example.com" />
        </label>
        <label className="field">
          SMTP 端口
          <input type="number" required min={1} max={65535} value={form.smtpPort}
            onChange={(e) => setForm({ ...form, provider: "custom", smtpPort: Number(e.target.value) })} />
        </label>
        <label className="field ssl-field">
          <input type="checkbox" checked={form.imapSsl} disabled /> <span>IMAP TLS/SSL（必需）</span>
        </label>
        <label className="field ssl-field">
          <input type="checkbox" checked={form.smtpSsl} disabled /> <span>{form.smtpSsl ? "SMTP TLS/SSL（必需）" : "SMTP STARTTLS（必需）"}</span>
        </label>
        <p className="muted">IMAP 与 SMTP 均要求加密连接；保存后会在后台自动验证连接。</p>
        <label className="field">
          密码 {account && <small>留空则不修改</small>}
          <input
            required={!account}
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </label>
        <div className="field signatures-field">
          <div className="field-label" id="signature-label">
            签名 <small>支持 Markdown；发送时会以 HTML 邮件格式呈现。</small>
          </div>
          {signatures.length > 0 && (
            <div className="signature-list" aria-label="签名列表">
              {signatures.map((item) => (
                <article className={`signature-card ${item.id === defaultSignatureId ? "is-default" : ""}`} key={item.id}>
                  <div className="signature-card-heading">
                    <input
                      aria-label={`签名名称 ${item.name}`}
                      value={item.name}
                      onChange={(event) => updateSignature(item.id, { name: event.target.value })}
                    />
                    {item.id === defaultSignatureId && <span className="signature-badge">默认</span>}
                  </div>
                  <textarea
                    aria-label={`签名内容 ${item.name}`}
                    rows={3}
                    value={item.markdown}
                    onChange={(event) => updateSignature(item.id, { markdown: event.target.value })}
                  />
                  <div className="signature-preview" aria-label={`签名预览 ${item.name}`} dangerouslySetInnerHTML={{ __html: sanitizeEmailHtml(renderEmailMarkdown(item.markdown)) }} />
                  <div className="signature-card-actions">
                    <button
                      type="button"
                      className="text-button"
                      disabled={item.id === defaultSignatureId}
                      onClick={() => setDefaultSignatureId(item.id)}
                    >
                      {item.id === defaultSignatureId ? "默认签名" : "设为默认"}
                    </button>
                    <button type="button" className="danger text-button" onClick={() => removeSignature(item.id)}>
                      删除
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          <div className="signature-add" aria-label="添加签名">
            <input
              aria-label="新签名名称"
              placeholder="签名名称（可选）"
              value={newSignatureName}
              onChange={(event) => setNewSignatureName(event.target.value)}
            />
            <textarea
              aria-label="新签名内容"
              rows={3}
              placeholder="输入 Markdown 签名内容"
              value={newSignatureMarkdown}
              onChange={(event) => setNewSignatureMarkdown(event.target.value)}
            />
            <button type="button" className="secondary" onClick={addSignature} disabled={!newSignatureMarkdown.trim()}>
              添加签名
            </button>
          </div>
          {!signatures.length && <p className="muted">尚未设置签名；你可以在这里添加一个或多个签名。</p>}
        </div>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="button" className="secondary" onClick={onClose}>
            取消
          </button>
          {account && onRequestDelete && (
            <button type="button" className="danger" onClick={() => onRequestDelete(account)}>
              删除账户
            </button>
          )}
          <button disabled={busy}>{busy ? "保存中…" : "保存账户"}</button>
        </footer>
      </form>
    </section>
  );
}

export function LlmSettingsForm({
  settings,
  onSave,
  onClose,
  toast,
}: {
  settings: LlmSettings;
  onSave(settings: LlmSettings): void;
  onClose(): void;
  toast(message: string): void;
}) {
  const dialog = useDialog(onClose);
  const [form, setForm] = useState({
    enabled: settings.enabled,
    baseUrl: settings.baseUrl,
    model: settings.model,
    defaultLanguage: settings.defaultLanguage,
    apiKey: "",
    summaryThreshold: settings.summaryThreshold,
  });
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(
    settings.lastTestStatus && !["never", "unknown"].includes(settings.lastTestStatus)
      ? { ok: settings.lastTestStatus === "ok" || settings.lastTestStatus === "connected", message: settings.lastTestStatus }
      : null,
  );
  const effectiveTestStatus = testResult ? (testResult.ok ? "ok" : "failed") : settings.lastTestStatus;
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const baseUrl = form.baseUrl.trim();
    const model = form.model.trim();
    if (form.enabled) {
      if (!baseUrl) return setError("请填写 Base URL");
      try {
        const parsed = new URL(baseUrl);
        if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
      } catch {
        return setError("Base URL 必须是 http(s) 地址");
      }
      if (!model) return setError("请填写模型名称");
    }
    if (!Number.isFinite(form.summaryThreshold) || form.summaryThreshold < 0)
      return setError("摘要阈值必须是大于等于 0 的数字");
    setBusy(true);
    setError("");
    try {
      const saved = await api.saveLlmSettings({
        enabled: form.enabled,
        baseUrl,
        model,
        defaultLanguage: form.defaultLanguage,
        summaryThreshold: form.summaryThreshold,
        apiKey: form.apiKey || undefined,
      });
      onSave(saved);
      toast("LLM 摘要设置已保存。");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setTesting(true);
    setError("");
    setTestResult(null);
    try {
      const result = await api.testLlmConnection({
        enabled: form.enabled,
        baseUrl: form.baseUrl.trim(),
        model: form.model.trim(),
        summaryThreshold: form.summaryThreshold,
        apiKey: form.apiKey || undefined,
      });
      const ok = Boolean(result.ok);
      setTestResult({ ok, message: result.error || result.status || result.last_test_status || (ok ? "连接成功" : "连接失败") });
      if (ok) toast("LLM 连接测试成功。");
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof Error ? err.message : "连接测试失败" });
    } finally {
      setTesting(false);
    }
  };
  return (
    <section ref={dialog} className="sheet llm-settings" role="dialog" aria-modal="true" aria-labelledby="llm-settings-title">
      <header>
        <div>
          <p className="eyebrow">摘要服务</p>
          <h2 id="llm-settings-title">LLM 设置</h2>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="关闭">×</button>
      </header>
      <form onSubmit={save}>
        <label className="setting-toggle">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
          />
          <span><strong>启用邮件摘要</strong><small>新邮件会在后台异步生成摘要。</small></span>
        </label>
        <label className="field">
          Base URL
          <input
            data-dialog-initial-focus
            required={form.enabled}
            type="url"
            inputMode="url"
            autoComplete="url"
            value={form.baseUrl}
            onChange={(event) => setForm({ ...form, baseUrl: event.target.value })}
            placeholder="https://api.openai.com/v1"
          />
        </label>
        <label className="field">
          模型
          <input required={form.enabled} autoComplete="off" value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="gpt-4o-mini" />
        </label>
        <label className="field">
          摘要语言
          <select aria-label="摘要语言" value={form.defaultLanguage} onChange={(event) => setForm({ ...form, defaultLanguage: event.target.value })}>
            <option value="zh_CN">简体中文</option>
            <option value="zh_TW">繁體中文</option>
            <option value="en_US">English</option>
            <option value="ja_JP">日本語</option>
            <option value="ko_KR">한국어</option>
            <option value="fr_FR">Français</option>
            <option value="de_DE">Deutsch</option>
            <option value="es_ES">Español</option>
          </select>
          <small>摘要中的标题、行动项和其他说明将使用此语言。</small>
        </label>
        <label className="field">
          API key <small>{settings.apiKeyConfigured ? "已配置；留空则保留原 key" : "仅发送到服务器保存，不会回显"}</small>
          <input type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder={settings.apiKeyConfigured ? "••••••••" : "sk-…"} />
        </label>
        <label className="field">
          摘要阈值（正文字符数）
          <input type="number" min={0} step={1} value={form.summaryThreshold} onChange={(event) => setForm({ ...form, summaryThreshold: Number(event.target.value) })} />
        </label>
        <div className="llm-test-row">
          <button type="button" className="secondary" disabled={testing || busy} onClick={() => void test()}>
            {testing ? "测试中…" : "测试连接"}
          </button>
          {testResult && <span className={`llm-test-result ${testResult.ok ? "ok" : "failed"}`} role="status">{testResult.ok ? "连接成功" : testResult.message}</span>}
        </div>
        <div className="llm-health" aria-label="LLM 状态">
          <span>状态：<strong>{effectiveTestStatus === "ok" || effectiveTestStatus === "connected" ? "已连接" : effectiveTestStatus === "failed" ? "失败" : "未测试"}</strong></span>
          <span>失败统计：<strong>{settings.failedCount}</strong></span>
          {settings.lastError && <span className="inline-error">最近失败：{settings.lastError}</span>}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <footer>
          <button type="button" className="secondary" onClick={onClose}>取消</button>
          <button disabled={busy}>{busy ? "保存中…" : "保存设置"}</button>
        </footer>
      </form>
    </section>
  );
}

export function Composer({
  account,
  accounts,
  initial,
  onClose,
  toast,
  telegramUserId,
}: {
  account: Account;
  accounts?: Account[];
  initial?: Partial<ComposePayload>;
  onClose(): void;
  toast(message: string): void;
  telegramUserId: number | null;
}) {
  const senderOptions = accounts?.filter(accountIsUsable) ?? (accountIsUsable(account) ? [account] : []);
  const [selectedAccountId, setSelectedAccountId] = useState(initial?.accountId ?? account.id);
  const selectedAccount = accounts?.find((item) =>
    item.id === selectedAccountId
      && (Boolean(initial?.replyTo || initial?.forwardOf) || accountIsUsable(item)),
  ) ?? senderOptions[0] ?? account;
  const [to, setTo] = useState(initial?.to ?? []);
  const [cc, setCc] = useState(initial?.cc ?? []);
  const [bcc, setBcc] = useState(initial?.bcc ?? []);
  const [subject, setSubject] = useState(initial?.subject ?? "");
  const initialSignatureItem = selectedAccountSignature(selectedAccount.signature);
  const initialSignature = initialSignatureItem?.markdown ?? "";
  const previousSignature = useRef(initialSignatureItem);
  const [selectedSignatureId, setSelectedSignatureId] = useState<string | null>(initialSignatureItem?.id ?? null);
  const hasExplicitBody = typeof initial?.body === "string";
  const [insertedSignatureId, setInsertedSignatureId] = useState<string | null>(hasExplicitBody ? null : initialSignatureItem?.id ?? null);
  const [body, setBody] = useState(hasExplicitBody ? initial?.body ?? "" : initialSignature);
  const [attachments, setAttachments] = useState<Attachment[]>(
    initial?.attachments ?? [],
  );
  const [showMore, setShowMore] = useState(
    Boolean(initial?.cc?.length || initial?.bcc?.length),
  );
  const [preview, setPreview] = useState(false);
  const [status, setStatus] = useState<DeliveryStatus | null>(null);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const sendKey = useRef<string | null>(null);
  const saveTextDraft = (announce = true) => {
    if (!saveComposeDraft(telegramUserId, { activeAccountId: selectedAccount.id, to, cc, bcc, subject, body })) {
      setError(telegramUserId === null ? "身份尚未确认，无法在此设备保存草稿。" : "草稿保存失败，请保持此窗口打开。");
      return false;
    }
    if (announce)
      toast(attachments.length ? "草稿已保存；附件需重新添加。" : "草稿已保存。");
    return true;
  };
  const chooseSender = (accountId: string) => {
    const next = senderOptions.find((item) => item.id === accountId) ?? account;
    const nextSignature = selectedAccountSignature(next.signature);
    setSelectedAccountId(accountId);
    const oldSignature = previousSignature.current?.markdown ?? "";
    if (insertedSignatureId && oldSignature && body.trim() === oldSignature.trim()) {
      setBody(nextSignature?.markdown ?? "");
      setInsertedSignatureId(nextSignature?.id ?? null);
    } else if (!body.trim()) {
      setBody(nextSignature?.markdown ?? "");
      setInsertedSignatureId(nextSignature?.id ?? null);
    } else if (insertedSignatureId && oldSignature) {
      setBody(replaceTrailingSignature(body, oldSignature, nextSignature?.markdown ?? ""));
      setInsertedSignatureId(nextSignature?.id ?? null);
    }
    setSelectedSignatureId(nextSignature?.id ?? null);
    previousSignature.current = nextSignature;
  };
  const signatureSet = parseAccountSignatures(selectedAccount.signature);
  const selectedSignature = signatureSet.items.find((item) => item.id === selectedSignatureId) ?? null;
  const insertSelectedSignature = () => {
    if (!selectedSignature) return;
    if (insertedSignatureId) {
      const previous = signatureSet.items.find((item) => item.id === insertedSignatureId);
      if (previous) setBody((value) => replaceTrailingSignature(value, previous.markdown, selectedSignature.markdown));
    } else {
      setBody((value) => appendSignature(value, selectedSignature.markdown));
    }
    setInsertedSignatureId(selectedSignature.id);
  };
  const deleteInsertedSignature = () => {
    if (!insertedSignatureId) return;
    const inserted = signatureSet.items.find((item) => item.id === insertedSignatureId);
    if (inserted) setBody((value) => removeTrailingSignature(value, inserted.markdown));
    setInsertedSignatureId(null);
  };
  const chooseSignature = (id: string) => {
    const nextId = id || null;
    const next = signatureSet.items.find((item) => item.id === nextId) ?? null;
    if (insertedSignatureId) {
      const current = signatureSet.items.find((item) => item.id === insertedSignatureId);
      if (current) {
        setBody((value) => next ? replaceTrailingSignature(value, current.markdown, next.markdown) : removeTrailingSignature(value, current.markdown));
        setInsertedSignatureId(next?.id ?? null);
      }
    }
    setSelectedSignatureId(next?.id ?? null);
  };
  const persistAndClose = () => {
    if (saveTextDraft()) onClose();
  };
  const dialog = useDialog(persistAndClose);
  const appendMarkdown = (before: string, after = before) =>
    setBody(
      (value) =>
        `${value}${value && !value.endsWith("\n") ? "\n" : ""}${before}文字${after}`,
    );
  const send = async () => {
    if (!to.length) return setError("至少需要一位收件人");
    setStatus("sending");
    setError("");
    try {
      const result = await api.send(
        {
          accountId: selectedAccount.id,
          to,
          cc,
          bcc,
          subject,
          body,
          attachments,
          replyTo: initial?.replyTo,
          forwardOf: initial?.forwardOf,
        },
        (sendKey.current ??= api.idempotencyKey()),
      );
      setStatus(result.status);
      if (result.status === "sent") {
        saveRecentAccountId(telegramUserId, selectedAccount.id);
        clearComposeDraft(telegramUserId);
        toast("邮件已发送。");
        window.setTimeout(onClose, 700);
      } else if (result.status === "queued") {
        saveRecentAccountId(telegramUserId, selectedAccount.id);
        if (saveTextDraft(false)) {
          toast("邮件已排队发送，草稿已保存。");
          window.setTimeout(onClose, 700);
        }
      } else toast(`邮件状态：${result.status}`);
    } catch (err) {
      setStatus("failed");
      setError(err instanceof Error ? err.message : "发送失败");
    }
  };
  const content = (
    <>
      {accounts && (
        <label className="field sender-field">
          发件账户
          <select
            value={selectedAccount.id}
            disabled={Boolean(initial?.replyTo || initial?.forwardOf) || status === "sending"}
            onChange={(event) => chooseSender(event.target.value)}
          >
            {senderOptions.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.email}</option>)}
          </select>
        </label>
      )}
      <TokenInput
        label="收件人"
        values={to}
        onChange={setTo}
        disabled={status === "sending"}
      />
      <button
        className="text-button"
        type="button"
        onClick={() => setShowMore(!showMore)}
      >
        {showMore ? "隐藏抄送/密送" : "添加抄送/密送"}
      </button>
      {showMore && (
        <>
          <TokenInput
            label="抄送"
            values={cc}
            onChange={setCc}
            disabled={status === "sending"}
          />
          <TokenInput
            label="密送"
            values={bcc}
            onChange={setBcc}
            disabled={status === "sending"}
          />
        </>
      )}
      <label className="field">
        主题
        <input
          value={subject}
          disabled={status === "sending"}
          onChange={(e) => setSubject(e.target.value)}
        />
      </label>
      <div className="field">
        <label htmlFor="body">正文</label>
        <div className="toolbar" aria-label="Markdown 工具栏">
          <button type="button" onClick={() => appendMarkdown("**")}>
            粗体
          </button>
          <button type="button" onClick={() => appendMarkdown("*")}>
            斜体
          </button>
          <button type="button" onClick={() => appendMarkdown("- ", "")}>
            列表
          </button>
          <button type="button" onClick={() => appendMarkdown("[链接](", ")")}>
            链接
          </button>
        </div>
        <textarea
          id="body"
          rows={9}
          value={body}
          disabled={status === "sending"}
          onChange={(e) => setBody(e.target.value)}
          placeholder="支持 Markdown"
        />
      </div>
      {signatureSet.items.length > 0 && (
        <div className="compose-signature" aria-label="邮件签名">
          <label htmlFor="compose-signature-select">签名</label>
          <div className="compose-signature-actions">
            <select
              id="compose-signature-select"
              aria-label="签名"
              value={selectedSignatureId ?? ""}
              disabled={status === "sending"}
              onChange={(event) => chooseSignature(event.target.value)}
            >
              <option value="">不使用签名</option>
              {signatureSet.items.map((item) => <option value={item.id} key={item.id}>{item.name}{item.id === signatureSet.defaultId ? "（默认）" : ""}</option>)}
            </select>
            <button type="button" className="secondary" onClick={insertSelectedSignature} disabled={!selectedSignature || status === "sending"}>插入签名</button>
            <button type="button" className="text-button" onClick={deleteInsertedSignature} disabled={!insertedSignatureId || status === "sending"}>删除签名</button>
          </div>
          {selectedSignature && <p className="muted compose-signature-status">{insertedSignatureId === selectedSignature.id ? `已插入“${selectedSignature.name}”` : `将插入“${selectedSignature.name}”`}</p>}
        </div>
      )}
      <div className="attachment">
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const selected = Array.from(e.target.files ?? []);
            const tooLarge = selected.find((file) => file.size > 25 * 1024 * 1024);
            if (tooLarge) {
              setError(`“${tooLarge.name}” 超过单文件 25 MiB 限制。`);
              e.currentTarget.value = "";
              return;
            }
            setError("");
            setAttachments((old) => [
              ...old,
              ...selected.map((file) => ({ name: file.name, size: file.size, file })),
            ]);
            e.currentTarget.value = "";
          }}
        />
        <button
          type="button"
          className="secondary"
          onClick={() => fileInput.current?.click()}
        >
          添加附件
        </button>
        {attachments.map((file, index) => (
          <span className="token" key={`${file.name}${index}`}>
            {file.name}（{formatBytes(file.size)}）{" "}
            <button
              type="button"
              aria-label={`移除 ${file.name}`}
              onClick={() =>
                setAttachments((items) => items.filter((_, i) => i !== index))
              }
            >
              ×
            </button>
          </span>
        ))}
      </div>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      {status && (
        <p className={`delivery ${status}`}>
          状态：{status === "sending" ? "发送中" : status}
        </p>
      )}
    </>
  );
  return (
    <section
      ref={dialog}
      className="sheet compose"
      role="dialog"
      aria-modal="true"
      aria-labelledby="compose-title"
    >
      <header>
        <h2 id="compose-title">
          {initial?.replyTo
            ? "回复邮件"
            : initial?.forwardOf
              ? "转发邮件"
              : "新邮件"}
        </h2>
        <button className="icon-button" onClick={persistAndClose} aria-label="关闭">
          ×
        </button>
      </header>
      {preview ? (
        <>
          <div className="preview">
            <p>
              <b>发件账户：</b>
              {selectedAccount.email}
            </p>
            <p>
              <b>收件人：</b>
              {to.join(", ")}
            </p>
            {cc.length > 0 && (
              <p>
                <b>抄送：</b>
                {cc.join(", ")}
              </p>
            )}
            {bcc.length > 0 && (
              <p>
                <b>密送：</b>
                {bcc.join(", ")}
              </p>
            )}
            {attachments.length > 0 && (
              <div className="preview-attachments">
                <b>附件：</b>
                <ul>
                  {attachments.map((file, index) => (
                    <li key={`${file.name}${index}`}>{file.name}（{formatBytes(file.size)}）</li>
                  ))}
                </ul>
              </div>
            )}
            <h3>{subject || "（无主题）"}</h3>
            <div
              className="preview-body"
              dangerouslySetInnerHTML={{ __html: sanitizeEmailHtml(renderEmailMarkdown(body)) }}
            />
          </div>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <footer>
            <button
              className="secondary"
              onClick={() => setPreview(false)}
              disabled={status === "sending"}
            >
              返回编辑
            </button>
            <button onClick={send} disabled={status === "sending"}>
              {status === "sending"
                ? "发送中…"
                : status === "failed" || status === "ambiguous"
                  ? "重试发送"
                  : "确认发送"}
            </button>
          </footer>
        </>
      ) : (
        <>
          <div className="compose-body">{content}</div>
          <footer>
            <button className="secondary" onClick={persistAndClose}>
              保存草稿并关闭
            </button>
            <button
              onClick={() => {
                if (to.length) {
                  setError("");
                  setPreview(true);
                } else setError("至少需要一位收件人");
              }}
            >
              预览
            </button>
          </footer>
        </>
      )}
    </section>
  );
}

export function ThreadDetail({
  thread,
  account,
  onBack,
  onOpenLlmSettings,
  onCompose,
  onDelete,
  onRetrySummary,
  canDelete,
}: {
  thread: Thread;
  account: Account;
  onBack(): void;
  onOpenLlmSettings(): void;
  onCompose(initial: Partial<ComposePayload>): void;
  onDelete(): void;
  onRetrySummary(emailId?: string): void;
  canDelete: boolean;
}) {
  const latest = thread.messages.at(-1);
  const replyTo = latest ? replyRecipients(latest, account.email, false) : [];
  const replyAll = latest ? replyAllRecipients(latest, account.email) : { to: [], cc: [] };
  const hasSummaryData = Boolean(thread.summary || thread.summaryStatus || thread.messages.some((message) => message.summary || message.summaryStatus));
  const [view, setView] = useState<"summary" | "body">(() => hasSummaryData ? "summary" : "body");
  const [externalImagesAllowed, setExternalImagesAllowed] = useState(false);
  useEffect(() => {
    setExternalImagesAllowed(false);
  }, [thread.id]);
  const threadSummaryStatus = normalizeSummaryStatus(thread.summaryStatus ?? latest?.summaryStatus, thread.summary ?? latest?.summary);
  const renderBody = (message: Thread["messages"][number]) => {
    const rendered = message.html || renderEmailMarkdown(message.body);
    const inlineImageUrls = (message.inlineAssets ?? []).reduce<Record<string, string>>((urls, asset) => {
      // Only construct the URL from numeric ids issued by our API.  Provider
      // HTML and arbitrary API fields must never become a DOM URL.
      if (/^\d+$/.test(message.id) && /^\d+$/.test(asset.id) && asset.contentId.trim()) {
        urls[asset.contentId] = `/api/v1/emails/${message.id}/inline-assets/${asset.id}`;
      }
      return urls;
    }, {});
    const hasExternalImages = hasExternalEmailImages(rendered);
    return (
      <>
        {hasExternalImages && !externalImagesAllowed && (
          <div className="external-images-notice">
            <span>外部图片已拦截，加载后可能向发件方暴露你的访问。</span>
            <button type="button" className="secondary" onClick={() => setExternalImagesAllowed(true)}>
              显示外部图片
            </button>
          </div>
        )}
        <div
          className={`message-body ${message.html ? "email-html" : "email-markdown"}`}
          dangerouslySetInnerHTML={{
            __html: sanitizeEmailHtml(rendered, {
              allowExternalImages: externalImagesAllowed,
              inlineImageUrls,
            }),
          }}
        />
      </>
    );
  };
  return (
    <main className="detail">
      <header className="topbar">
        <button className="secondary" onClick={onBack}>
          ← 收件箱
        </button>
        <button className="secondary" onClick={onOpenLlmSettings}>
          LLM 设置
        </button>
        <button
          className="danger"
          disabled={!canDelete}
          title={canDelete ? "删除此线程" : "删除只能从 Telegram Mini App 发起"}
          onClick={onDelete}
        >
          删除
        </button>
      </header>
      <h1>{thread.subject}</h1>
      <p className="muted">{thread.participants.join(" · ")}</p>
      <div className="summary-switch" role="tablist" aria-label="邮件内容视图">
        <button type="button" role="tab" aria-selected={view === "summary"} className={view === "summary" ? "selected" : "secondary"} onClick={() => setView("summary")}>摘要</button>
        <button type="button" role="tab" aria-selected={view === "body"} className={view === "body" ? "selected" : "secondary"} onClick={() => setView("body")}>正文</button>
      </div>
      {thread.messages.map((message) => (
        <article className="message" key={message.id}>
          <div className="avatar">{initials(message.from)}</div>
          <div>
            <strong>{message.from}</strong>
            <time>{fmtDate(message.date)}</time>
            {view === "body" ? renderBody(message) : (
              <div className={`summary-panel ${normalizeSummaryStatus(message.summaryStatus ?? thread.summaryStatus, message.summary ?? thread.summary)}`}>
                <div className="summary-meta">
                  <span className="summary-status">{summaryStatusLabel(message.summaryStatus ?? thread.summaryStatus, message.summary ?? thread.summary)}</span>
                  {message.priority && <span className="summary-tag">{message.priority}</span>}
                  {message.category && <span className="summary-tag">{message.category}</span>}
                </div>
                {message.summary ? <div className="message-body summary-body" dangerouslySetInnerHTML={{ __html: sanitizeSummaryHtml(message.summary) }} /> : <p className="muted">{normalizeSummaryStatus(message.summaryStatus ?? thread.summaryStatus) === "failed" ? `摘要生成失败${message.summaryErrorCode ? `（${message.summaryErrorCode}）` : "。"}` : normalizeSummaryStatus(message.summaryStatus ?? thread.summaryStatus) === "generating" ? "摘要正在生成，请稍候。" : "摘要尚未生成。"}</p>}
                {normalizeSummaryStatus(message.summaryStatus ?? thread.summaryStatus, message.summary) === "failed" && <button type="button" className="secondary retry-summary" onClick={() => onRetrySummary(message.id)}>重试摘要</button>}
              </div>
            )}
          </div>
        </article>
      ))}
      {!thread.messages.length && hasSummaryData && (
        <section className={`summary-panel ${threadSummaryStatus}`}>
          <div className="summary-meta"><span className="summary-status">{summaryStatusLabel(thread.summaryStatus, thread.summary)}</span></div>
          {thread.summary ? <div className="message-body summary-body" dangerouslySetInnerHTML={{ __html: sanitizeSummaryHtml(thread.summary) }} /> : <p className="muted">{threadSummaryStatus === "failed" ? `摘要生成失败${thread.summaryErrorCode ? `（${thread.summaryErrorCode}）` : "。"}` : "摘要尚未生成。"}</p>}
          {threadSummaryStatus === "failed" && <button type="button" className="secondary retry-summary" onClick={() => onRetrySummary(latest?.id)}>重试摘要</button>}
        </section>
      )}
      <div className="actions">
        <button
          onClick={() =>
            onCompose({
              accountId: account.id,
              to: replyTo,
              subject: prefixedSubject(thread.subject, "Re"),
              replyTo: thread.id,
            })
          }
        >
          回复
        </button>
        <button
          onClick={() =>
            onCompose({
              accountId: account.id,
              to: replyAll.to,
              cc: replyAll.cc,
              subject: prefixedSubject(thread.subject, "Re"),
              replyTo: thread.id,
            })
          }
        >
          回复全部
        </button>
        <button
          className="secondary"
          onClick={() =>
            onCompose({
              accountId: account.id,
              subject: prefixedSubject(thread.subject, "Fwd"),
              body: `\n\n--- 转发的邮件 ---\n${thread.messages.at(-1)?.body ?? ""}`,
              forwardOf: thread.id,
            })
          }
        >
          转发
        </button>
      </div>
    </main>
  );
}

export function emailAddress(value: string) {
  return (value.match(/<([^>]+)>/)?.[1] ?? value).trim();
}

export function prefixedSubject(subject: string, prefix: "Re" | "Fwd") {
  return new RegExp(`^${prefix}:\\s*`, "i").test(subject)
    ? subject
    : `${prefix}: ${subject}`;
}

export function replyRecipients(
  message: Thread["messages"][number],
  accountEmail: string,
  includeAll: boolean,
) {
  const own = emailAddress(accountEmail).toLowerCase();
  const sender = emailAddress(message.from);
  const candidates = includeAll
    ? [sender, ...message.to, ...(message.cc ?? [])]
    : sender.toLowerCase() === own
      ? [...message.to]
      : [sender];
  const seen = new Set<string>();
  return candidates.map(emailAddress).filter((value) => {
    const normalized = value.toLowerCase();
    if (!value || normalized === own || seen.has(normalized)) return false;
    seen.add(normalized);
    return true;
  });
}

export function replyAllRecipients(
  message: Thread["messages"][number],
  accountEmail: string,
) {
  const own = emailAddress(accountEmail).toLowerCase();
  const seen = new Set<string>([own]);
  const unique = (values: string[]) =>
    values.map(emailAddress).filter((value) => {
      const normalized = value.toLowerCase();
      if (!value || seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    });
  return {
    to: unique([message.from, ...message.to]),
    cc: unique(message.cc ?? []),
  };
}

export function DeleteConfirmation({
  provider,
  count = 1,
  onClose,
  onConfirm,
  deleting = false,
}: {
  provider?: string;
  count?: number;
  onClose(): void;
  onConfirm(): void;
  deleting?: boolean;
}) {
  const dialog = useDialog(onClose);
  return (
    <section
      ref={dialog}
      className="sheet confirm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-title"
      aria-describedby="delete-description"
    >
      <h2 id="delete-title">{count > 1 ? `确认删除 ${count} 个线程？` : "确认删除线程？"}</h2>
      <p id="delete-description">
        后台会先通过 <b>{provider}</b> 从邮箱服务器删除线程中的所有邮件，再删除对应的 Telegram Topic。
        任一步骤失败都会保留进度并自动重试；删除只能从 Telegram Mini App 发起。
      </p>
      <footer>
        <button type="button" className="secondary" data-dialog-initial-focus onClick={onClose}>
          取消
        </button>
        <button type="button" className="danger" onClick={onConfirm} disabled={deleting}>
          {deleting ? "提交中…" : "确认删除"}
        </button>
      </footer>
    </section>
  );
}

export function AccountDeleteConfirmation({
  account,
  onClose,
  onConfirm,
  deleting = false,
}: {
  account: Account;
  onClose(): void;
  onConfirm(purgeData: boolean): void;
  deleting?: boolean;
}) {
  const dialog = useDialog(onClose);
  const [purgeData, setPurgeData] = useState(false);
  return (
    <section ref={dialog} className="sheet confirm" role="dialog" aria-modal="true" aria-labelledby="account-delete-title" aria-describedby="account-delete-description">
      <h2 id="account-delete-title">确认删除账户？</h2>
      <p id="account-delete-description">
        将移除 <b>{account.email}</b> 的登录配置并停止同步。
      </p>
      <label className="delete-option">
        <input type="checkbox" checked={purgeData} onChange={(event) => setPurgeData(event.target.checked)} />
        <span><strong>同时删除本地邮件、草稿、联系人和 Telegram Topic</strong><small>此操作不可恢复；不会删除邮箱服务商服务器中的原始邮件。</small></span>
      </label>
      <footer>
        <button type="button" className="secondary" data-dialog-initial-focus onClick={onClose}>取消</button>
        <button type="button" className="danger" onClick={() => onConfirm(purgeData)} disabled={deleting}>{deleting ? "提交中…" : "确认删除"}</button>
      </footer>
    </section>
  );
}

export function App() {
  useTelegramTheme();
  const miniAppRoute = useRef(getMiniAppRoute());
  const miniAppRouteHandled = useRef(false);
  const [ready, setReady] = useState(false);
  const [telegramUserId, setTelegramUserId] = useState<number | null>(null);
  const [authChecking, setAuthChecking] = useState(true);
  const [authError, setAuthError] = useState("");
  const [setupNotice, setSetupNotice] = useState("");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeAccountId, setActiveAccountId] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [loading, setLoading] = useState(true);
  const [llmSettings, setLlmSettings] = useState<LlmSettings | null>(null);
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);
  const [accountModal, setAccountModal] = useState<Account | false | null>(
    null,
  );
  const [accountDeleteTarget, setAccountDeleteTarget] = useState<Account | null>(null);
  const [accountDeleting, setAccountDeleting] = useState(false);
  const [compose, setCompose] = useState<Partial<ComposePayload> | null>(null);
  const [current, setCurrent] = useState<Thread | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [setupCode, setSetupCode] = useState("");
  const [setupError, setSetupError] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedThreadIds, setSelectedThreadIds] = useState<Set<string>>(() => new Set());
  const [deleteTargetIds, setDeleteTargetIds] = useState<string[]>([]);
  const deleteKeys = useRef(new Map<string, string>());
  const longPressTimer = useRef<number | null>(null);
  const longPressTriggered = useRef(false);
  const longPressPoint = useRef<{ x: number; y: number } | null>(null);
  const automaticAuthAttempted = useRef(false);
  const refreshController = useRef<AbortController | null>(null);
  const refreshInFlight = useRef(false);
  const refreshPending = useRef(false);
  const refreshSequence = useRef(0);
  const detailController = useRef<AbortController | null>(null);
  const detailSequence = useRef(0);
  const appShell = useRef<HTMLDivElement>(null);
  const modalOpen = accountModal !== null || accountDeleteTarget !== null || compose !== null || deleteOpen || llmSettingsOpen;
  useModalBackground(appShell, modalOpen);
  const activeAccount = useMemo(
    () =>
      accounts.find((account) => account.id === activeAccountId && accountIsUsable(account)) ??
      accounts.find(accountIsUsable),
    [accounts, activeAccountId],
  );
  const composeAccount = useMemo(
    () => accounts.find((account) => account.id === compose?.accountId) ?? activeAccount,
    [accounts, compose?.accountId, activeAccount],
  );
  const currentAccount = useMemo(
    () => accounts.find((account) => account.id === current?.accountId) ?? activeAccount,
    [accounts, current?.accountId, activeAccount],
  );
  const groupedThreads = useMemo(() => groupThreadsByAccount(threads, accounts), [threads, accounts]);
  const deleteProvider = useMemo(() => {
    if (deleteTargetIds.length !== 1) return "对应邮箱服务";
    const target = threads.find((thread) => thread.id === deleteTargetIds[0]);
    return accounts.find((account) => account.id === target?.accountId)?.provider ?? currentAccount?.provider;
  }, [accounts, currentAccount?.provider, deleteTargetIds, threads]);
  const canDelete = Boolean(getTelegramInitData());
  const refresh = async (options: { silent?: boolean } = {}) => {
    // A single polling loop is shared by account verification and summary
    // status.  AbortController handles an unmount/navigation while the guard
    // prevents two 2.5-second ticks from racing each other.
    if (refreshInFlight.current) {
      // A manual refresh requested while the 2.5s silent poll is in flight
      // should run once that request settles instead of appearing to do
      // nothing. Silent ticks themselves remain coalesced.
      if (!options.silent) refreshPending.current = true;
      return;
    }
    const sequence = ++refreshSequence.current;
    const controller = new AbortController();
    refreshController.current = controller;
    refreshInFlight.current = true;
    if (!options.silent) setLoading(true);
    try {
      const [nextAccounts, nextThreads] = await Promise.all([
        api.accounts(controller.signal),
        api.threads(controller.signal),
      ]);
      if (sequence !== refreshSequence.current || controller.signal.aborted) return;
      setAccounts([...nextAccounts]);
      setThreads([...nextThreads]);
      if (!options.silent && typeof api.llmSettings === "function") {
        try {
          const settings = await api.llmSettings(controller.signal);
          if (settings && sequence === refreshSequence.current && !controller.signal.aborted) setLlmSettings(settings);
        } catch (error) {
          if (!controller.signal.aborted && !options.silent) setToast(error instanceof Error ? error.message : "无法加载 LLM 设置");
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && sequence === refreshSequence.current) {
        setToast(error instanceof Error ? error.message : "加载失败");
      }
    } finally {
      if (sequence === refreshSequence.current) {
        refreshInFlight.current = false;
        refreshController.current = null;
        if (!options.silent) setLoading(false);
        if (refreshPending.current && !controller.signal.aborted) {
          refreshPending.current = false;
          void refresh();
        }
      }
    }
  };
  const bootstrapAuthentication = async () => {
    setAuthChecking(true);
    setAuthError("");
    setSetupNotice("");
    try {
      const status = await api.authStatus();
      if (status.authenticated) {
        setTelegramUserId(status.telegram_user_id ?? null);
        setReady(true);
        return;
      }
      const initData = getTelegramInitData();
      if (!initData) {
        setReady(false);
        return;
      }
      try {
        const session = await api.session(initData);
        if (session.authenticated) {
          setTelegramUserId(session.telegram_user_id ?? null);
          setReady(true);
          return;
        }
        setSetupNotice("此 Telegram 用户尚未绑定。仅首次绑定需要 setup code；已绑定用户会在所有设备上自动登录。");
        setReady(false);
      } catch (error) {
        if (error instanceof ApiError && [401, 403].includes(error.status)) {
          setSetupNotice("此 Telegram 用户尚未绑定。仅首次绑定需要 setup code；已绑定用户会在所有设备上自动登录。");
          setReady(false);
          return;
        }
        throw error;
      }
    } catch (error) {
      setReady(false);
      setAuthError(error instanceof Error ? error.message : "无法连接认证服务。");
    } finally {
      setAuthChecking(false);
    }
  };
  useEffect(() => {
    if (automaticAuthAttempted.current) return;
    automaticAuthAttempted.current = true;
    void bootstrapAuthentication();
  }, []);
  useEffect(() => {
    setActiveAccountId(loadRecentAccountId(telegramUserId));
  }, [telegramUserId]);
  useEffect(() => {
    if (ready) void refresh();
    else if (!authChecking) setLoading(false);
  }, [ready, authChecking]);
  useEffect(() => () => {
    refreshController.current?.abort();
    detailController.current?.abort();
    refreshPending.current = false;
    refreshSequence.current += 1;
    detailSequence.current += 1;
  }, []);
  const hasCheckingAccounts = accounts.some((account) => account.connectionStatus === "checking");
  const pendingStatuses = ["pending", "generating", "running", "queued", "processing", "retrying", "scheduled", "waiting", "in_progress", "in-progress"];
  const hasPendingSummaries = threads.some((thread) => pendingStatuses.includes(String(thread.summaryStatus ?? "").toLowerCase()))
    || Boolean(current?.messages.some((message) => pendingStatuses.includes(String(message.summaryStatus ?? "").toLowerCase())));
  useEffect(() => {
    if (!ready || (!hasCheckingAccounts && !hasPendingSummaries)) return;
    const timer = window.setInterval(() => {
      void refresh({ silent: true });
      if (current && hasPendingSummaries && typeof api.thread === "function") {
        detailController.current?.abort();
        const controller = new AbortController();
        detailController.current = controller;
        const sequence = ++detailSequence.current;
        void api.thread(current.id, controller.signal).then((detail) => {
          if (sequence !== detailSequence.current || controller.signal.aborted) return;
          setCurrent((old) => old ? {
            ...old,
            ...detail,
            accountId: old.accountId,
            subject: detail.subject === "（无主题）" ? old.subject : detail.subject,
          } : old);
        }).catch((error) => {
          if (!(error instanceof DOMException && error.name === "AbortError")) return;
        }).finally(() => {
          if (detailController.current === controller) detailController.current = null;
        });
      }
    }, 2500);
    return () => {
      window.clearInterval(timer);
      detailController.current?.abort();
      detailController.current = null;
      detailSequence.current += 1;
    };
  }, [ready, hasCheckingAccounts, hasPendingSummaries, current?.id]);
  const bind = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      const session = await api.setup(setupCode, getTelegramInitData());
      setTelegramUserId(session.telegram_user_id ?? null);
      setReady(true);
    } catch (error) {
      setSetupError(error instanceof Error ? error.message : "绑定失败");
    }
  };
  const openComposer = (initial: Partial<ComposePayload> = {}) => {
    if (initial.replyTo || initial.forwardOf) {
      setCompose(initial);
      return;
    }
    const saved = loadComposeDraft(telegramUserId);
    if (saved) {
      const savedAccount = accounts.find((item) => item.id === saved.activeAccountId && accountIsUsable(item)) ?? activeAccount;
      setCompose({
        accountId: savedAccount?.id,
        to: saved.to,
        cc: saved.cc,
        bcc: saved.bcc,
        subject: saved.subject,
        body: saved.body,
      });
      return;
    }
    setCompose(initial);
  };
  const loadThread = async (threadId: string, fallback?: Thread) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    const sequence = ++detailSequence.current;
    try {
      const detail = await api.thread(threadId, controller.signal);
      if (sequence !== detailSequence.current || controller.signal.aborted) return null;
      setCurrent({
        ...detail,
        subject: detail.subject === "（无主题）" ? fallback?.subject ?? detail.subject : detail.subject,
        accountId: fallback?.accountId ?? detail.accountId,
        preview: detail.preview || fallback?.preview || "",
        summary: detail.summary ?? fallback?.summary,
        summaryStatus: detail.summaryStatus ?? fallback?.summaryStatus,
        summaryErrorCode: detail.summaryErrorCode ?? fallback?.summaryErrorCode,
      });
      return detail;
    } catch (error) {
      if (!controller.signal.aborted && !(error instanceof DOMException && error.name === "AbortError")) {
        setToast(error instanceof Error ? error.message : "无法加载邮件线程");
      }
      return null;
    } finally {
      if (detailController.current === controller) detailController.current = null;
    }
  };
  const openThread = async (thread: Thread) => {
    await loadThread(thread.id, thread);
  };
  useEffect(() => {
    if (!ready || miniAppRouteHandled.current) return;
    const route = miniAppRoute.current;
    if (route.action === "compose") {
      miniAppRouteHandled.current = true;
      openComposer();
      return;
    }
    if (!route.threadId) {
      miniAppRouteHandled.current = true;
      return;
    }
    // The detail endpoint is allowed to open a thread outside the Mini App's
    // bounded 50-row console; the list is only used to enrich its preview.
    miniAppRouteHandled.current = true;
    void loadThread(route.threadId, threads.find((thread) => thread.id === route.threadId)).then((detail) => {
      if (!detail || !["reply", "forward"].includes(route.action)) return;
      const account = accounts.find((item) => item.id === detail.accountId);
      const latest = detail.messages.at(-1);
      if (!account || !latest) return;
      if (route.action === "reply") {
        openComposer({
          accountId: account.id,
          to: replyRecipients(latest, account.email, false),
          subject: prefixedSubject(detail.subject, "Re"),
          replyTo: detail.id,
        });
      } else {
        openComposer({
          accountId: account.id,
          subject: prefixedSubject(detail.subject, "Fwd"),
          body: `\n\n--- 转发的邮件 ---\n${latest.body ?? ""}`,
          forwardOf: detail.id,
        });
      }
    });
  }, [accounts, ready, threads]);
  const retrySummary = async (emailId?: string) => {
    if (!current || typeof api.retrySummary !== "function") {
      setToast("当前服务不支持重试摘要。");
      return;
    }
    try {
      const targetEmailId = emailId ?? current.messages.at(-1)?.id ?? current.id;
      await api.retrySummary(targetEmailId);
      setCurrent((old) => old ? {
        ...old,
        summaryStatus: "generating",
        messages: old.messages.map((message) => message.id === targetEmailId ? { ...message, summaryStatus: "generating" } : message),
      } : old);
      setThreads((items) => items.map((thread) => thread.id === current.id ? { ...thread, summaryStatus: "generating" } : thread));
      setToast("摘要已重新排队，完成后会自动刷新。");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "摘要重试失败");
    }
  };
  const openLlmSettings = async () => {
    if (llmSettings) {
      setLlmSettingsOpen(true);
      return;
    }
    if (typeof api.llmSettings !== "function") {
      setToast("当前服务不支持 LLM 设置。");
      return;
    }
    try {
      const settings = await api.llmSettings();
      if (!settings) {
        setToast("LLM 设置暂不可用。");
        return;
      }
      setLlmSettings(settings);
      setLlmSettingsOpen(true);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "无法加载 LLM 设置");
    }
  };
  const toggleThreadSelection = (id: string) => {
    setSelectedThreadIds((items) => {
      const next = new Set(items);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const activateSelection = (id: string) => {
    setSelectionMode(true);
    setSelectedThreadIds(new Set([id]));
    setToast("已进入批量选择，继续点击可多选邮件。");
  };
  const clearLongPress = () => {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  };
  const startLongPress = (id: string, x = 0, y = 0) => {
    clearLongPress();
    longPressTriggered.current = false;
    longPressPoint.current = { x, y };
    longPressTimer.current = window.setTimeout(() => {
      longPressTriggered.current = true;
      longPressPoint.current = null;
      activateSelection(id);
    }, 520);
  };
  const cancelLongPress = () => {
    clearLongPress();
    longPressPoint.current = null;
  };
  const exitSelection = () => {
    cancelLongPress();
    longPressTriggered.current = false;
    setSelectionMode(false);
    setSelectedThreadIds(new Set());
  };
  const deleteThreads = async () => {
    const targets = deleteTargetIds.length ? deleteTargetIds : current ? [current.id] : [];
    if (!targets.length) return;
    setDeleting(true);
    try {
      const results = await Promise.allSettled(
        targets.map((id) => {
          const key = deleteKeys.current.get(id) ?? api.idempotencyKey();
          deleteKeys.current.set(id, key);
          return api.removeThread(id, key);
        }),
      );
      const acceptedStatuses = new Set(["accepted", "queued", "running", "deleting", "succeeded", "deleted"]);
      const deletedIds = targets.filter((_, index) => {
        const result = results[index];
        return result?.status === "fulfilled" && acceptedStatuses.has(result.value.status);
      });
      const failedCount = targets.length - deletedIds.length;
      // The durable delete continues in the background. Leave the topic detail
      // as soon as Telegramail accepts the request, before Telegram removes the
      // currently open Topic and the native client dismisses its host window.
      setThreads((items) => items.filter((item) => !deletedIds.includes(item.id)));
      if (current && deletedIds.includes(current.id)) setCurrent(null);
      deletedIds.forEach((id) => deleteKeys.current.delete(id));
      setSelectedThreadIds((items) => new Set([...items].filter((id) => !deletedIds.includes(id))));
      if (!failedCount) {
        setSelectionMode(false);
        setDeleteTargetIds([]);
        setToast(targets.length > 1 ? `已提交删除 ${targets.length} 个线程。` : "删除请求已提交，正在后台同步邮箱与 Telegram Topic。");
      } else {
        setToast(`${deletedIds.length} 个线程已提交删除，${failedCount} 个线程失败，请重试。`);
      }
      setDeleteOpen(false);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "删除请求失败");
    } finally {
      setDeleting(false);
    }
  };
  const deleteAccount = async (purgeData: boolean) => {
    if (!accountDeleteTarget) return;
    setAccountDeleting(true);
    try {
      const result = await api.removeAccount(accountDeleteTarget.id, purgeData);
      setAccounts((items) => items.filter((item) => item.id !== accountDeleteTarget.id));
      const recentId = loadRecentAccountId(telegramUserId);
      if (activeAccountId === accountDeleteTarget.id || recentId === accountDeleteTarget.id) {
        const fallback = accounts.find((item) => item.id !== accountDeleteTarget.id && accountIsUsable(item));
        setActiveAccountId(fallback?.id ?? null);
        if (fallback) saveRecentAccountId(telegramUserId, fallback.id);
        else clearRecentAccountId(telegramUserId);
      }
      setAccountDeleteTarget(null);
      setAccountModal(null);
      setToast(purgeData
        ? (result.status === "deleted"
          ? "账户及本地数据已删除。"
          : ["failed", "error"].includes(String(result.status).toLowerCase())
            ? "账户清理失败，请稍后重试。"
            : "账户清理已排队，Topic 删除完成后会清理本地数据。")
        : "账户已删除，历史邮件数据已保留。");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "账户删除失败");
    } finally {
      setAccountDeleting(false);
    }
  };
  if (authChecking) return <main className="setup"><Loading /></main>;
  if (authError)
    return (
      <main className="setup">
        <section className="card">
          <p className="eyebrow">Telegramail</p>
          <h1>无法恢复会话</h1>
          <p className="inline-error" role="alert">{authError}</p>
          <button onClick={() => void bootstrapAuthentication()}>重试</button>
        </section>
      </main>
    );
  if (!ready)
    return (
      <main className="setup">
        <section className="card">
          <p className="eyebrow">Telegramail</p>
          <h1>绑定邮箱服务</h1>
          <p>仅首次绑定管理员需要 setup code。绑定后，同一 Telegram 账号在其他设备上会自动登录。</p>
          {setupNotice && <p role="status">{setupNotice}</p>}
          <form onSubmit={bind}>
            <label className="field">
              Setup code
              <input
                autoFocus
                value={setupCode}
                onChange={(e) => setSetupCode(e.target.value.trim())}
              />
            </label>
            {setupError && (
              <p className="inline-error" role="alert">
                {setupError}
              </p>
            )}
            <button disabled={!setupCode}>绑定</button>
          </form>
        </section>
      </main>
    );
  return (
    <>
      <div className="app-shell" ref={appShell}>
        {current && currentAccount ? (
          <ThreadDetail
            thread={current}
            account={currentAccount}
            onBack={() => setCurrent(null)}
            onOpenLlmSettings={() => void openLlmSettings()}
            onCompose={openComposer}
            canDelete={canDelete}
            onRetrySummary={(emailId) => void retrySummary(emailId)}
            onDelete={() => {
              setDeleteTargetIds([current.id]);
              setDeleteOpen(true);
            }}
          />
        ) : (
          <>
            <header className="app-header">
              <div>
                <p className="eyebrow">Telegramail</p>
                <h1>收件箱</h1>
              </div>
              <div className="header-actions">
                <button onClick={() => openComposer()} disabled={!activeAccount}>
                  写邮件
                </button>
                <button className="secondary" onClick={() => void openLlmSettings()}>
                  LLM 设置
                </button>
              </div>
            </header>
            <main className="inbox">
              <section className="accounts">
                <div className="section-heading">
                  <h2>账户</h2>
                  <button
                    className="text-button"
                    onClick={() => setAccountModal(false)}
                  >
                    添加
                  </button>
                </div>
                {loading ? (
                  <Loading />
                ) : (
                  accounts.map((account) => (
                    <article className="account" key={account.id}>
                      <div className="avatar">{initials(account.name)}</div>
                      <div>
                        <strong>{account.name}</strong>
                        <small>
                          {account.email} · {account.provider === "custom" ? "IMAP / SMTP" : providerPresets[account.provider as Exclude<ProviderPreset, "custom">]?.label ?? account.provider}
                        </small>
                        <span className={`status ${account.connectionStatus ?? account.status}`}>
                          {(account.connectionStatus ?? account.status) === "checking"
                            ? "正在后台验证连接…"
                            : (account.connectionStatus ?? account.status) === "connected"
                              ? "IMAP / SMTP 已连接"
                              : (account.connectionStatus ?? account.status) === "failed"
                                ? `连接暂不可用${account.connectionError ? `（${account.connectionError}）` : ""}`
                                : account.credentialConfigured ? "等待后台验证" : "尚未配置凭据"}
                        </span>
                      </div>
                      <div className="account-actions">
                        <button
                          className="text-button"
                          onClick={() => setAccountModal(account)}
                        >
                          编辑
                        </button>
                      </div>
                    </article>
                  ))
                )}
                {!loading && !accounts.length && (
                  <p className="empty">尚未添加账户。</p>
                )}
              </section>
              <section className="thread-list" aria-label="邮件线程">
                <div className="section-heading">
                  {selectionMode ? (
                    <div className="selection-heading" role="status" aria-live="polite">
                      <strong>已选择 {selectedThreadIds.size} 个线程</strong>
                      <button className="text-button" onClick={exitSelection}>取消</button>
                    </div>
                  ) : (
                    <h2>邮件</h2>
                  )}
                  {!selectionMode && (
                    <small className="selection-hint">长按邮件可批量选择</small>
                  )}
                  <div className="section-actions">
                    {selectionMode && (
                      <button
                        className="danger text-button"
                        disabled={!canDelete || !selectedThreadIds.size || deleting}
                        title={canDelete ? "删除已选择的线程" : "删除只能从 Telegram Mini App 发起"}
                        onClick={() => {
                          setDeleteTargetIds(Array.from(selectedThreadIds));
                          setDeleteOpen(true);
                        }}
                      >
                        删除
                      </button>
                    )}
                    <button className="text-button" onClick={() => void refresh()}>
                      刷新
                    </button>
                  </div>
                </div>
                {loading ? (
                  <Loading />
                ) : groupedThreads.length ? (
                  groupedThreads.map(({ accountId, account, threads: accountThreads }) => (
                    <section className="thread-account-group" key={accountId} aria-labelledby={`account-heading-${accountId}`}>
                      <div className="thread-account-heading">
                        <span className="avatar avatar-small">{initials(account?.name ?? account?.email ?? "?")}</span>
                        <span>
                          <strong id={`account-heading-${accountId}`}>{account?.name ?? "其他账户"}</strong>
                          <small>{account?.email ?? "未识别账户"} · {accountThreads.length} 封线程</small>
                        </span>
                      </div>
                      {accountThreads.map((thread) => {
                        const selected = selectedThreadIds.has(thread.id);
                        return (
                          <button
                            className={`thread ${thread.unread ? "unread" : ""} ${selected ? "selected" : ""}`}
                            key={thread.id}
                            aria-pressed={selectionMode ? selected : undefined}
                            onPointerDown={(event) => {
                              if (event.pointerType === "touch" && Number.isFinite(event.pointerId)) {
                                try {
                                  event.currentTarget.setPointerCapture(event.pointerId);
                                } catch {
                                  // Some embedded WebViews expose Pointer Events without capture support.
                                }
                              }
                              startLongPress(thread.id, event.clientX, event.clientY);
                            }}
                            onPointerMove={(event) => {
                              const point = longPressPoint.current;
                              if (point && Math.hypot(event.clientX - point.x, event.clientY - point.y) > 12) cancelLongPress();
                            }}
                            onPointerUp={(event) => {
                              if (event.pointerType === "touch" && Number.isFinite(event.pointerId)) {
                                try {
                                  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                                    event.currentTarget.releasePointerCapture(event.pointerId);
                                  }
                                } catch {
                                  // See the matching capture guard above.
                                }
                              }
                              cancelLongPress();
                            }}
                            onPointerCancel={cancelLongPress}
                            onTouchStart={(event) => {
                              const touch = event.touches[0];
                              if (touch) startLongPress(thread.id, touch.clientX, touch.clientY);
                            }}
                            onTouchMove={(event) => {
                              const touch = event.touches[0];
                              const point = longPressPoint.current;
                              if (touch && point && Math.hypot(touch.clientX - point.x, touch.clientY - point.y) > 12) {
                                cancelLongPress();
                              }
                            }}
                            onTouchEnd={cancelLongPress}
                            onTouchCancel={cancelLongPress}
                            onContextMenu={(event) => {
                              event.preventDefault();
                              cancelLongPress();
                              if (!selectionMode) activateSelection(thread.id);
                            }}
                            onClick={() => {
                              if (longPressTriggered.current) {
                                longPressTriggered.current = false;
                                return;
                              }
                              if (selectionMode) {
                                toggleThreadSelection(thread.id);
                                return;
                              }
                              void openThread(thread);
                            }}
                          >
                            {selectionMode && <span className="selection-indicator" aria-hidden="true">{selected ? "✓" : ""}</span>}
                            <span className="avatar">{initials(thread.participants[0] ?? account?.name ?? account?.email)}</span>
                            <span>
                              <strong>{thread.subject}</strong>
                              <small className="thread-preview">{thread.summary ? summaryPlainText(thread.summary) : thread.preview}</small>
                              {thread.summaryStatus && normalizeSummaryStatus(thread.summaryStatus, thread.summary) !== "generated" && <small className={`summary-list-status ${normalizeSummaryStatus(thread.summaryStatus, thread.summary)}`}>{summaryStatusLabel(thread.summaryStatus, thread.summary)}</small>}
                              <small>{fmtDate(thread.date)}</small>
                            </span>
                          </button>
                        );
                      })}
                    </section>
                  ))
                ) : (
                  <p className="empty">没有邮件。</p>
                )}
              </section>
            </main>
          </>
        )}
      </div>
      {accountModal !== null && (
        <div className="scrim">
          <AccountForm
            account={accountModal || undefined}
            onClose={() => setAccountModal(null)}
            toast={setToast}
            onRequestDelete={(target) => {
              setAccountModal(null);
              setAccountDeleteTarget(target);
            }}
            onSave={(saved) =>
              setAccounts((items) =>
                items.some((item) => item.id === saved.id)
                  ? items.map((item) => (item.id === saved.id ? saved : item))
                  : [...items, saved],
              )
            }
          />
        </div>
      )}
      {llmSettingsOpen && llmSettings && (
        <div className="scrim">
          <LlmSettingsForm
            settings={llmSettings}
            onClose={() => setLlmSettingsOpen(false)}
            toast={setToast}
            onSave={(saved) => setLlmSettings(saved)}
          />
        </div>
      )}
      {compose && composeAccount && (
        <div className="scrim">
          <Composer
            account={composeAccount}
            accounts={accounts}
            initial={compose}
            onClose={() => setCompose(null)}
            toast={setToast}
            telegramUserId={telegramUserId}
          />
        </div>
      )}
      {deleteOpen && deleteTargetIds.length > 0 && (
        <div className="scrim">
          <DeleteConfirmation
            count={deleteTargetIds.length}
            provider={deleteProvider}
            deleting={deleting}
            onClose={() => {
              setDeleteOpen(false);
              setDeleteTargetIds([]);
            }}
            onConfirm={() => void deleteThreads()}
          />
        </div>
      )}
      {accountDeleteTarget && (
        <div className="scrim">
          <AccountDeleteConfirmation
            account={accountDeleteTarget}
            deleting={accountDeleting}
            onClose={() => setAccountDeleteTarget(null)}
            onConfirm={(purgeData) => void deleteAccount(purgeData)}
          />
        </div>
      )}
      <Toast message={toast} onClose={() => setToast(null)} />
    </>
  );
}

const rootElement = document.getElementById("root");
if (rootElement) {
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

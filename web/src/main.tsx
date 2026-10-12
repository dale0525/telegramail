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

/* Shared utility strings. The visual language (grouped inset cards, hairline
   strokes, pill controls) comes from the Stitch design system
   "Telegram Mini App Email Workspace". */
const cardClass =
  "rounded-xl border border-outline-variant/60 bg-surface-container-lowest shadow-[0_1px_3px_rgba(0,0,0,0.04),0_4px_12px_rgba(0,0,0,0.02)]";
const primaryButtonClass =
  "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl bg-primary-container px-4 text-sm font-semibold text-on-primary-container transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100";
const secondaryButtonClass =
  "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl border border-outline-variant bg-surface-container-lowest px-4 text-sm font-semibold text-on-surface-variant transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100";
const dangerButtonClass =
  "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl border border-error/40 bg-error-container px-4 text-sm font-semibold text-on-error-container transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100";
const textButtonClass =
  "inline-flex min-h-9 items-center gap-1 rounded-lg px-2 text-sm font-semibold text-primary-container transition active:bg-primary-container/10 disabled:cursor-not-allowed disabled:opacity-50";
const fieldLabelClass = "grid gap-1.5 text-sm font-semibold text-on-surface";
const inputClass =
  "min-h-11 w-full rounded-xl border border-outline-variant bg-surface-container-lowest px-3 text-sm font-normal text-on-surface outline-none transition placeholder:text-outline focus:border-primary-container";
const sectionLabelClass =
  "text-[11px] font-semibold tracking-[0.06em] text-on-surface-variant uppercase";

function useTelegramTheme() {
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    const apply = () => {
      // The Mini App palette is a fixed design system; Telegram only selects
      // the light or dark variant, which the stylesheet keys off data-theme.
      document.documentElement.dataset.theme = app?.colorScheme ?? "light";
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
    <div
      className="fixed right-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] left-3 z-30 mx-auto flex max-w-[480px] items-center gap-2 rounded-xl bg-inverse-surface px-3.5 py-3 text-sm text-inverse-on-surface shadow-[0_10px_28px_rgba(0,0,0,0.25)] sm:left-auto sm:mx-0 sm:w-auto sm:max-w-[min(28rem,calc(100%-1.5rem))]"
      role="status"
    >
      <span className="flex-1">{message}</span>
      <button
        className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-lg leading-none text-inverse-on-surface/80 transition active:bg-inverse-on-surface/15"
        aria-label="关闭提示"
        onClick={onClose}
      >
        ×
      </button>
    </div>
  ) : null;
}
function Loading() {
  return (
    <div className="flex justify-center py-6" role="status" aria-label="加载中">
      <span className="size-6 animate-spin rounded-full border-[3px] border-primary-container/25 border-t-primary-container" />
    </div>
  );
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
    <div className="relative z-20 grid gap-1.5">
      <label className="text-sm font-semibold text-on-surface" htmlFor={inputId}>
        {label}
      </label>
      <div
        className="flex min-h-11 flex-wrap items-center gap-1.5 rounded-xl border border-outline-variant bg-surface-container-lowest p-1.5"
        onClick={() => input.current?.focus()}
      >
        {values.map((email) => (
          <span
            className="inline-flex max-w-full items-center gap-0.5 rounded-lg bg-primary-fixed py-1 pr-1 pl-2.5 text-xs font-medium text-on-primary-fixed-variant"
            key={email}
          >
            <span className="break-all">{email}</span>
            <button
              className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-base leading-none transition active:bg-primary-container/15"
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
          className="min-h-9 min-w-28 flex-1 border-0 bg-transparent px-1.5 text-sm text-on-surface outline-none placeholder:text-outline"
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
        <p id={`${label}-error`} className="m-0 text-xs font-medium text-error" role="alert">
          {error}
        </p>
      )}
      {matches.length > 0 && (
        <ul
          id={listboxId}
          className={`absolute top-full right-0 left-0 z-30 mt-1 max-h-64 list-none overflow-auto rounded-xl border border-outline-variant bg-surface-container-lowest p-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.16)]`}
          role="listbox"
          aria-label="联系人建议"
        >
          {matches.map((contact, index) => (
            <li
              className="flex min-h-10 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-2 text-sm aria-selected:bg-primary-fixed"
              id={`${listboxId}-${index}`}
              key={contact.email}
              role="option"
              aria-selected={index === selected}
              onMouseDown={(e) => {
                e.preventDefault();
                add(contact.email);
              }}
            >
              <strong className="font-semibold">{contact.name}</strong>
              <span className="text-on-surface-variant">{contact.email}</span>
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
      className={`mx-auto flex max-h-[calc(100vh-1.5rem)] w-full max-w-[520px] flex-col overflow-hidden rounded-t-2xl border border-outline-variant/60 bg-surface-container-lowest sm:rounded-2xl ${cardClass}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-title"
    >
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-outline-variant/60 bg-surface-container-lowest/90 px-4 py-3 backdrop-blur-md">
        <h2 className="m-0 text-lg font-semibold" id="account-title">
          {account ? "编辑账户" : "添加账户"}
        </h2>
        <button
          className="inline-flex size-9 items-center justify-center rounded-lg text-2xl leading-none text-on-surface-variant transition active:bg-surface-container"
          onClick={onClose}
          aria-label="关闭"
        >
          ×
        </button>
      </header>
      <form className="grid gap-4 overflow-y-auto p-4" onSubmit={submit}>
        <label className={fieldLabelClass}>
          显示名称
          <input
            className={inputClass}
            data-dialog-initial-focus
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </label>
        <label className={fieldLabelClass}>
          邮箱
          <input
            className={inputClass}
            required
            type="email"
            autoComplete="email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
        </label>
        <label className={fieldLabelClass}>
          服务商
          <select
            className={inputClass}
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
        <div className="grid gap-1.5">
          <label className="text-sm font-semibold text-on-surface" htmlFor="imap-server">
            IMAP 服务器
          </label>
          <input
            className={inputClass}
            id="imap-server"
            required
            autoComplete="off"
            value={form.imapServer}
            onChange={(e) => setForm({ ...form, provider: "custom", imapServer: e.target.value })}
            placeholder="imap.example.com"
          />
        </div>
        <label className={fieldLabelClass}>
          IMAP 端口
          <input className={inputClass} type="number" required min={1} max={65535} value={form.imapPort}
            onChange={(e) => setForm({ ...form, provider: "custom", imapPort: Number(e.target.value) })} />
        </label>
        <label className={fieldLabelClass}>
          SMTP 服务器
          <input className={inputClass} required autoComplete="off" value={form.smtpServer}
            onChange={(e) => setForm({ ...form, provider: "custom", smtpServer: e.target.value })}
            placeholder="smtp.example.com" />
        </label>
        <label className={fieldLabelClass}>
          SMTP 端口
          <input className={inputClass} type="number" required min={1} max={65535} value={form.smtpPort}
            onChange={(e) => setForm({ ...form, provider: "custom", smtpPort: Number(e.target.value) })} />
        </label>
        <label className="flex items-center gap-2.5 rounded-xl border border-outline-variant/60 bg-surface-container-low px-3 py-2.5 text-sm font-medium text-on-surface-variant">
          <input className="size-4 accent-primary-container" type="checkbox" checked={form.imapSsl} disabled />
          <span>IMAP TLS/SSL（必需）</span>
        </label>
        <label className="flex items-center gap-2.5 rounded-xl border border-outline-variant/60 bg-surface-container-low px-3 py-2.5 text-sm font-medium text-on-surface-variant">
          <input className="size-4 accent-primary-container" type="checkbox" checked={form.smtpSsl} disabled />
          <span>{form.smtpSsl ? "SMTP TLS/SSL（必需）" : "SMTP STARTTLS（必需）"}</span>
        </label>
        <p className="m-0 text-xs text-on-surface-variant">
          IMAP 与 SMTP 均要求加密连接；保存后会在后台自动验证连接。
        </p>
        <label className={fieldLabelClass}>
          <span className="flex flex-wrap items-baseline gap-1.5">
            密码
            {account && <small className="text-xs font-normal text-on-surface-variant">留空则不修改</small>}
          </span>
          <input
            className={inputClass}
            required={!account}
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </label>
        <div className="grid gap-3">
          <div className="grid gap-1" id="signature-label">
            <span className="text-sm font-semibold text-on-surface">签名</span>
            <small className="text-xs font-normal text-on-surface-variant">
              支持 Markdown；发送时会以 HTML 邮件格式呈现。
            </small>
          </div>
          {signatures.length > 0 && (
            <div className="grid gap-2.5" aria-label="签名列表">
              {signatures.map((item) => (
                <article
                  className={`grid min-w-0 gap-2.5 rounded-xl border p-3 ${
                    item.id === defaultSignatureId
                      ? "border-primary-container/50 bg-primary-fixed/40"
                      : "border-outline-variant/60 bg-surface-container-low/60"
                  }`}
                  key={item.id}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      className={`${inputClass} min-w-0 flex-1`}
                      aria-label={`签名名称 ${item.name}`}
                      value={item.name}
                      onChange={(event) => updateSignature(item.id, { name: event.target.value })}
                    />
                    {item.id === defaultSignatureId && (
                      <span className="shrink-0 rounded-full bg-primary-container px-2.5 py-1 text-[11px] font-semibold text-on-primary-container">
                        默认
                      </span>
                    )}
                  </div>
                  <textarea
                    className={`${inputClass} min-w-0 py-2.5`}
                    aria-label={`签名内容 ${item.name}`}
                    rows={3}
                    value={item.markdown}
                    onChange={(event) => updateSignature(item.id, { markdown: event.target.value })}
                  />
                  <div
                    className="signature-preview rounded-xl border border-outline-variant/60 bg-surface-container-lowest px-3 py-2.5 text-sm"
                    aria-label={`签名预览 ${item.name}`}
                    dangerouslySetInnerHTML={{ __html: sanitizeEmailHtml(renderEmailMarkdown(item.markdown)) }}
                  />
                  <div className="flex flex-wrap items-center justify-end gap-1.5">
                    <button
                      type="button"
                      className={textButtonClass}
                      disabled={item.id === defaultSignatureId}
                      onClick={() => setDefaultSignatureId(item.id)}
                    >
                      {item.id === defaultSignatureId ? "默认签名" : "设为默认"}
                    </button>
                    <button
                      type="button"
                      className="inline-flex min-h-9 items-center rounded-lg px-2 text-sm font-semibold text-error transition active:bg-error-container/60 disabled:opacity-50"
                      onClick={() => removeSignature(item.id)}
                    >
                      删除
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          <div
            className="flex flex-wrap items-center gap-2.5 rounded-xl border border-dashed border-outline-variant p-3"
            aria-label="添加签名"
          >
            <input
              className={`${inputClass} min-w-0 flex-[1_1_12rem]`}
              aria-label="新签名名称"
              placeholder="签名名称（可选）"
              value={newSignatureName}
              onChange={(event) => setNewSignatureName(event.target.value)}
            />
            <textarea
              className={`${inputClass} min-w-0 flex-[2_1_16rem] py-2.5`}
              aria-label="新签名内容"
              rows={3}
              placeholder="输入 Markdown 签名内容"
              value={newSignatureMarkdown}
              onChange={(event) => setNewSignatureMarkdown(event.target.value)}
            />
            <button type="button" className={secondaryButtonClass} onClick={addSignature} disabled={!newSignatureMarkdown.trim()}>
              添加签名
            </button>
          </div>
          {!signatures.length && (
            <p className="m-0 text-xs text-on-surface-variant">
              尚未设置签名；你可以在这里添加一个或多个签名。
            </p>
          )}
        </div>
        {error && (
          <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
            {error}
          </p>
        )}
        <footer className="sticky bottom-0 -mx-4 -mb-4 flex flex-wrap items-center justify-end gap-2 border-t border-outline-variant/60 bg-surface-container-lowest/95 px-4 py-3 backdrop-blur-md">
          <button type="button" className={secondaryButtonClass} onClick={onClose}>
            取消
          </button>
          {account && onRequestDelete && (
            <button type="button" className={dangerButtonClass} onClick={() => onRequestDelete(account)}>
              删除账户
            </button>
          )}
          <button className={primaryButtonClass} disabled={busy}>
            {busy ? "保存中…" : "保存账户"}
          </button>
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
    <section
      ref={dialog}
      className={`mx-auto flex max-h-[calc(100vh-1.5rem)] w-full max-w-[520px] flex-col overflow-hidden rounded-t-2xl border border-outline-variant/60 bg-surface-container-lowest sm:rounded-2xl ${cardClass}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="llm-settings-title"
    >
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-outline-variant/60 bg-surface-container-lowest/90 px-4 py-3 backdrop-blur-md">
        <div className="grid gap-0.5">
          <p className="m-0 text-[11px] font-semibold tracking-[0.06em] text-secondary uppercase">摘要服务</p>
          <h2 className="m-0 text-lg font-semibold" id="llm-settings-title">LLM 设置</h2>
        </div>
        <button
          className="inline-flex size-9 items-center justify-center rounded-lg text-2xl leading-none text-on-surface-variant transition active:bg-surface-container"
          onClick={onClose}
          aria-label="关闭"
        >
          ×
        </button>
      </header>
      <form className="grid gap-4 overflow-y-auto p-4" onSubmit={save}>
        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-secondary/25 bg-secondary-fixed/40 p-3">
          <input
            className="mt-0.5 size-5 shrink-0 accent-secondary"
            type="checkbox"
            checked={form.enabled}
            onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
          />
          <span className="grid gap-0.5">
            <strong className="text-sm font-semibold text-on-surface">启用邮件摘要</strong>
            <small className="text-xs font-normal text-on-surface-variant">新邮件会在后台异步生成摘要。</small>
          </span>
        </label>
        <label className={fieldLabelClass}>
          Base URL
          <input
            className={inputClass}
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
        <label className={fieldLabelClass}>
          模型
          <input className={inputClass} required={form.enabled} autoComplete="off" value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="gpt-4o-mini" />
        </label>
        <label className={fieldLabelClass}>
          摘要语言
          <select className={inputClass} aria-label="摘要语言" value={form.defaultLanguage} onChange={(event) => setForm({ ...form, defaultLanguage: event.target.value })}>
            <option value="zh_CN">简体中文</option>
            <option value="zh_TW">繁體中文</option>
            <option value="en_US">English</option>
            <option value="ja_JP">日本語</option>
            <option value="ko_KR">한국어</option>
            <option value="fr_FR">Français</option>
            <option value="de_DE">Deutsch</option>
            <option value="es_ES">Español</option>
          </select>
          <small className="text-xs font-normal text-on-surface-variant">摘要中的标题、行动项和其他说明将使用此语言。</small>
        </label>
        <label className={fieldLabelClass}>
          <span className="flex flex-wrap items-baseline gap-1.5">
            API key
            <small className="text-xs font-normal text-on-surface-variant">
              {settings.apiKeyConfigured ? "已配置；留空则保留原 key" : "仅发送到服务器保存，不会回显"}
            </small>
          </span>
          <input className={inputClass} type="password" autoComplete="new-password" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder={settings.apiKeyConfigured ? "••••••••" : "sk-…"} />
        </label>
        <label className={fieldLabelClass}>
          摘要阈值（正文字符数）
          <input className={inputClass} type="number" min={0} step={1} value={form.summaryThreshold} onChange={(event) => setForm({ ...form, summaryThreshold: Number(event.target.value) })} />
        </label>
        <div className="flex min-h-11 flex-wrap items-center gap-2.5">
          <button type="button" className={secondaryButtonClass} disabled={testing || busy} onClick={() => void test()}>
            {testing ? "测试中…" : "测试连接"}
          </button>
          {testResult && (
            <span
              className={`text-xs font-medium break-all ${testResult.ok ? "text-tertiary" : "text-error"}`}
              role="status"
            >
              {testResult.ok ? "连接成功" : testResult.message}
            </span>
          )}
        </div>
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-outline-variant/60 bg-surface-container-low px-3 py-2.5 text-xs text-on-surface-variant"
          aria-label="LLM 状态"
        >
          <span>
            状态：
            <strong className="font-semibold text-on-surface">
              {effectiveTestStatus === "ok" || effectiveTestStatus === "connected" ? "已连接" : effectiveTestStatus === "failed" ? "失败" : "未测试"}
            </strong>
          </span>
          <span>
            失败统计：<strong className="font-semibold text-on-surface">{settings.failedCount}</strong>
          </span>
          {settings.lastError && <span className="font-medium text-error">最近失败：{settings.lastError}</span>}
        </div>
        {error && (
          <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
            {error}
          </p>
        )}
        <footer className="sticky bottom-0 -mx-4 -mb-4 flex flex-wrap items-center justify-end gap-2 border-t border-outline-variant/60 bg-surface-container-lowest/95 px-4 py-3 backdrop-blur-md">
          <button type="button" className={secondaryButtonClass} onClick={onClose}>取消</button>
          <button className={primaryButtonClass} disabled={busy}>{busy ? "保存中…" : "保存设置"}</button>
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
        <label className={fieldLabelClass}>
          发件账户
          <select
            className={inputClass}
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
        className={`${textButtonClass} self-start`}
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
      <label className={fieldLabelClass}>
        主题
        <input
          className={inputClass}
          value={subject}
          disabled={status === "sending"}
          onChange={(e) => setSubject(e.target.value)}
        />
      </label>
      <div className="grid gap-1.5">
        <label className="text-sm font-semibold text-on-surface" htmlFor="body">正文</label>
        <div className="flex flex-wrap gap-1.5" aria-label="Markdown 工具栏">
          <button
            className="inline-flex min-h-9 items-center rounded-lg bg-primary-fixed px-2.5 text-sm font-semibold text-on-primary-fixed-variant transition active:scale-[0.98]"
            type="button"
            onClick={() => appendMarkdown("**")}
          >
            粗体
          </button>
          <button
            className="inline-flex min-h-9 items-center rounded-lg bg-primary-fixed px-2.5 text-sm font-semibold text-on-primary-fixed-variant transition active:scale-[0.98]"
            type="button"
            onClick={() => appendMarkdown("*")}
          >
            斜体
          </button>
          <button
            className="inline-flex min-h-9 items-center rounded-lg bg-primary-fixed px-2.5 text-sm font-semibold text-on-primary-fixed-variant transition active:scale-[0.98]"
            type="button"
            onClick={() => appendMarkdown("- ", "")}
          >
            列表
          </button>
          <button
            className="inline-flex min-h-9 items-center rounded-lg bg-primary-fixed px-2.5 text-sm font-semibold text-on-primary-fixed-variant transition active:scale-[0.98]"
            type="button"
            onClick={() => appendMarkdown("[链接](", ")")}
          >
            链接
          </button>
        </div>
        <textarea
          className={`${inputClass} py-2.5 leading-relaxed`}
          id="body"
          rows={9}
          value={body}
          disabled={status === "sending"}
          onChange={(e) => setBody(e.target.value)}
          placeholder="支持 Markdown"
        />
      </div>
      {signatureSet.items.length > 0 && (
        <div
          className="grid gap-2 rounded-xl border border-outline-variant/60 bg-primary-fixed/30 p-3"
          aria-label="邮件签名"
        >
          <label className="text-sm font-semibold text-on-surface" htmlFor="compose-signature-select">签名</label>
          <div className="flex flex-wrap items-center gap-2">
            <select
              className={`${inputClass} min-w-0 flex-[1_1_12rem]`}
              id="compose-signature-select"
              aria-label="签名"
              value={selectedSignatureId ?? ""}
              disabled={status === "sending"}
              onChange={(event) => chooseSignature(event.target.value)}
            >
              <option value="">不使用签名</option>
              {signatureSet.items.map((item) => <option value={item.id} key={item.id}>{item.name}{item.id === signatureSet.defaultId ? "（默认）" : ""}</option>)}
            </select>
            <button type="button" className={secondaryButtonClass} onClick={insertSelectedSignature} disabled={!selectedSignature || status === "sending"}>插入签名</button>
            <button type="button" className={textButtonClass} onClick={deleteInsertedSignature} disabled={!insertedSignatureId || status === "sending"}>删除签名</button>
          </div>
          {selectedSignature && (
            <p className="m-0 text-xs text-on-surface-variant">
              {insertedSignatureId === selectedSignature.id ? `已插入“${selectedSignature.name}”` : `将插入“${selectedSignature.name}”`}
            </p>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
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
          className={secondaryButtonClass}
          onClick={() => fileInput.current?.click()}
        >
          添加附件
        </button>
        {attachments.map((file, index) => (
          <span
            className="inline-flex max-w-full items-center gap-0.5 rounded-lg bg-surface-container py-1 pr-1 pl-2.5 text-xs font-medium text-on-surface-variant"
            key={`${file.name}${index}`}
          >
            <span className="break-all">{file.name}（{formatBytes(file.size)}）</span>
            <button
              className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-base leading-none transition active:bg-surface-container-high"
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
        <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
          {error}
        </p>
      )}
      {status && (
        <p
          className={`m-0 text-sm font-semibold ${
            status === "sent" ? "text-tertiary" : status === "failed" || status === "ambiguous" ? "text-error" : "text-on-surface-variant"
          }`}
        >
          状态：{status === "sending" ? "发送中" : status}
        </p>
      )}
    </>
  );
  return (
    <section
      ref={dialog}
      className={`mx-auto flex max-h-[calc(100vh-1.5rem)] w-full max-w-[560px] flex-col overflow-hidden rounded-t-2xl border border-outline-variant/60 bg-surface-container-lowest sm:rounded-2xl ${cardClass}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="compose-title"
    >
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-outline-variant/60 bg-surface-container-lowest/90 px-4 py-3 backdrop-blur-md">
        <h2 className="m-0 text-lg font-semibold" id="compose-title">
          {initial?.replyTo
            ? "回复邮件"
            : initial?.forwardOf
              ? "转发邮件"
              : "新邮件"}
        </h2>
        <button
          className="inline-flex size-9 items-center justify-center rounded-lg text-2xl leading-none text-on-surface-variant transition active:bg-surface-container"
          onClick={persistAndClose}
          aria-label="关闭"
        >
          ×
        </button>
      </header>
      {preview ? (
        <>
          <div className="grid gap-2.5 overflow-y-auto p-4">
            <div className="grid gap-2 rounded-xl border border-outline-variant/60 bg-surface-container-low p-3.5 text-sm">
              <p className="m-0">
                <b className="font-semibold text-on-surface-variant">发件账户：</b>
                {selectedAccount.email}
              </p>
              <p className="m-0">
                <b className="font-semibold text-on-surface-variant">收件人：</b>
                {to.join(", ")}
              </p>
              {cc.length > 0 && (
                <p className="m-0">
                  <b className="font-semibold text-on-surface-variant">抄送：</b>
                  {cc.join(", ")}
                </p>
              )}
              {bcc.length > 0 && (
                <p className="m-0">
                  <b className="font-semibold text-on-surface-variant">密送：</b>
                  {bcc.join(", ")}
                </p>
              )}
            </div>
            {attachments.length > 0 && (
              <div className="grid gap-1.5 rounded-xl border border-outline-variant/60 p-3.5 text-sm">
                <b className="font-semibold text-on-surface-variant">附件：</b>
                <ul className="m-0 grid gap-1 pl-5">
                  {attachments.map((file, index) => (
                    <li key={`${file.name}${index}`}>{file.name}（{formatBytes(file.size)}）</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="rounded-xl border border-outline-variant/60 bg-surface-container-lowest p-4">
              <h3 className="mt-0 mb-2.5 text-base font-semibold">{subject || "（无主题）"}</h3>
              <div
                className="preview-body text-sm"
                dangerouslySetInnerHTML={{ __html: sanitizeEmailHtml(renderEmailMarkdown(body)) }}
              />
            </div>
            {error && (
              <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
                {error}
              </p>
            )}
          </div>
          <footer className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-outline-variant/60 bg-surface-container-lowest/95 px-4 py-3 backdrop-blur-md">
            <button
              className={secondaryButtonClass}
              onClick={() => setPreview(false)}
              disabled={status === "sending"}
            >
              返回编辑
            </button>
            <button className={primaryButtonClass} onClick={send} disabled={status === "sending"}>
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
          <div className="grid gap-3.5 overflow-y-auto p-4">{content}</div>
          <footer className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-outline-variant/60 bg-surface-container-lowest/95 px-4 py-3 backdrop-blur-md">
            <button className={secondaryButtonClass} onClick={persistAndClose}>
              保存草稿并关闭
            </button>
            <button
              className={primaryButtonClass}
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
          <div className="flex flex-wrap items-center gap-2.5 rounded-xl border border-outline-variant/60 bg-surface-container-low px-3 py-2.5 text-xs text-on-surface-variant">
            <span className="flex-1">外部图片已拦截，加载后可能向发件方暴露你的访问。</span>
            <button type="button" className={secondaryButtonClass} onClick={() => setExternalImagesAllowed(true)}>
              显示外部图片
            </button>
          </div>
        )}
        <div
          className={`text-sm ${message.html ? "email-html" : "email-markdown"}`}
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
    <main className="mx-auto grid w-full max-w-[560px] gap-3.5 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[calc(6rem+env(safe-area-inset-bottom))]">
      <header className="flex flex-wrap items-center gap-2">
        <button className={secondaryButtonClass} onClick={onBack}>
          ← 收件箱
        </button>
        <span className="flex-1" />
        <button className={secondaryButtonClass} onClick={onOpenLlmSettings}>
          LLM 设置
        </button>
        <button
          className={dangerButtonClass}
          disabled={!canDelete}
          title={canDelete ? "删除此线程" : "删除只能从 Telegram Mini App 发起"}
          onClick={onDelete}
        >
          删除
        </button>
      </header>
      <div className={`grid gap-2.5 p-4 ${cardClass}`}>
        <h1 className="m-0 text-xl leading-snug font-semibold tracking-[-0.015em]">{thread.subject}</h1>
        <p className="m-0 text-xs break-all text-on-surface-variant">{thread.participants.join(" · ")}</p>
      </div>
      <div
        className="flex gap-1 rounded-xl border border-outline-variant/60 bg-surface-container-low p-1"
        role="tablist"
        aria-label="邮件内容视图"
      >
        <button
          type="button"
          role="tab"
          aria-selected={view === "summary"}
          className={`min-h-9 flex-1 rounded-lg text-sm font-semibold transition ${
            view === "summary" ? "bg-surface-container-lowest text-primary-container shadow-[0_1px_3px_rgba(0,0,0,0.06)]" : "text-on-surface-variant"
          }`}
          onClick={() => setView("summary")}
        >
          摘要
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "body"}
          className={`min-h-9 flex-1 rounded-lg text-sm font-semibold transition ${
            view === "body" ? "bg-surface-container-lowest text-primary-container shadow-[0_1px_3px_rgba(0,0,0,0.06)]" : "text-on-surface-variant"
          }`}
          onClick={() => setView("body")}
        >
          正文
        </button>
      </div>
      {thread.messages.map((message) => {
        const status = normalizeSummaryStatus(message.summaryStatus ?? thread.summaryStatus, message.summary ?? thread.summary);
        return (
          <article className={`grid grid-cols-[auto_1fr] gap-3 p-4 ${cardClass}`} key={message.id}>
            <div className="flex size-10 items-center justify-center rounded-full bg-primary-fixed text-sm font-semibold text-on-primary-fixed-variant">
              {initials(message.from)}
            </div>
            <div className="grid min-w-0 gap-2.5">
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                <strong className="text-sm font-semibold break-all">{message.from}</strong>
                <time className="text-[11px] text-on-surface-variant">{fmtDate(message.date)}</time>
              </div>
              {view === "body" ? renderBody(message) : (
                <div
                  className={`grid gap-2.5 rounded-xl border p-3.5 ${
                    status === "failed"
                      ? "border-error/35 bg-error-container/40"
                      : status === "generating" || status === "pending"
                        ? "border-secondary/25 bg-secondary-fixed/40"
                        : "border-secondary/20 bg-secondary-fixed/25"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded-md bg-surface-container-lowest px-2 py-0.5 text-[11px] font-semibold text-secondary">
                      {summaryStatusLabel(message.summaryStatus ?? thread.summaryStatus, message.summary ?? thread.summary)}
                    </span>
                    {message.priority && (
                      <span className="rounded-md bg-surface-container-lowest px-2 py-0.5 text-[11px] font-medium text-on-surface-variant">
                        {message.priority}
                      </span>
                    )}
                    {message.category && (
                      <span className="rounded-md bg-surface-container-lowest px-2 py-0.5 text-[11px] font-medium text-on-surface-variant">
                        {message.category}
                      </span>
                    )}
                  </div>
                  {message.summary ? (
                    <div className="summary-body text-sm" dangerouslySetInnerHTML={{ __html: sanitizeSummaryHtml(message.summary) }} />
                  ) : (
                    <p className="m-0 text-sm text-on-surface-variant">
                      {status === "failed"
                        ? `摘要生成失败${message.summaryErrorCode ? `（${message.summaryErrorCode}）` : "。"}`
                        : status === "generating"
                          ? "摘要正在生成，请稍候。"
                          : "摘要尚未生成。"}
                    </p>
                  )}
                  {status === "failed" && (
                    <button type="button" className={`${secondaryButtonClass} self-start`} onClick={() => onRetrySummary(message.id)}>
                      重试摘要
                    </button>
                  )}
                </div>
              )}
            </div>
          </article>
        );
      })}
      {!thread.messages.length && hasSummaryData && (
        <section
          className={`grid gap-2.5 rounded-xl border p-4 ${
            threadSummaryStatus === "failed" ? "border-error/35 bg-error-container/40" : "border-secondary/25 bg-secondary-fixed/40"
          }`}
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="rounded-md bg-surface-container-lowest px-2 py-0.5 text-[11px] font-semibold text-secondary">
              {summaryStatusLabel(thread.summaryStatus, thread.summary)}
            </span>
          </div>
          {thread.summary ? (
            <div className="summary-body text-sm" dangerouslySetInnerHTML={{ __html: sanitizeSummaryHtml(thread.summary) }} />
          ) : (
            <p className="m-0 text-sm text-on-surface-variant">
              {threadSummaryStatus === "failed"
                ? `摘要生成失败${thread.summaryErrorCode ? `（${thread.summaryErrorCode}）` : "。"}`
                : "摘要尚未生成。"}
            </p>
          )}
          {threadSummaryStatus === "failed" && (
            <button type="button" className={`${secondaryButtonClass} self-start`} onClick={() => onRetrySummary(latest?.id)}>
              重试摘要
            </button>
          )}
        </section>
      )}
      <div className="fixed inset-x-0 bottom-0 z-20 mx-auto flex w-full max-w-[560px] gap-2 border-t border-outline-variant/60 bg-surface-container-lowest/95 px-3 pt-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] backdrop-blur-md">
        <button
          className={`${primaryButtonClass} flex-1`}
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
          className={`${secondaryButtonClass} flex-1`}
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
          className={`${secondaryButtonClass} flex-1`}
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
      className={`mx-auto grid w-full max-w-[420px] gap-3 rounded-2xl p-5 ${cardClass}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-title"
      aria-describedby="delete-description"
    >
      <h2 className="m-0 text-lg font-semibold" id="delete-title">
        {count > 1 ? `确认删除 ${count} 个线程？` : "确认删除线程？"}
      </h2>
      <p className="m-0 text-sm leading-relaxed text-on-surface-variant" id="delete-description">
        后台会先删除对应的 Telegram Topic，再通过 <b className="font-semibold text-on-surface">{provider}</b> 从邮箱服务器删除线程中的所有邮件。
        任一步骤失败都会保留进度并自动重试；删除只能从 Telegram Mini App 发起。
      </p>
      <footer className="mt-1 flex flex-wrap justify-end gap-2">
        <button type="button" className={secondaryButtonClass} data-dialog-initial-focus onClick={onClose}>
          取消
        </button>
        <button type="button" className={dangerButtonClass} onClick={onConfirm} disabled={deleting}>
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
    <section
      ref={dialog}
      className={`mx-auto grid w-full max-w-[420px] gap-3 rounded-2xl p-5 ${cardClass}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-delete-title"
      aria-describedby="account-delete-description"
    >
      <h2 className="m-0 text-lg font-semibold" id="account-delete-title">确认删除账户？</h2>
      <p className="m-0 text-sm text-on-surface-variant" id="account-delete-description">
        将移除 <b className="font-semibold text-on-surface">{account.email}</b> 的登录配置并停止同步。
      </p>
      <label className="flex cursor-pointer items-start gap-2.5 rounded-xl bg-error-container/50 p-3">
        <input
          className="mt-0.5 size-4 shrink-0 accent-error"
          type="checkbox"
          checked={purgeData}
          onChange={(event) => setPurgeData(event.target.checked)}
        />
        <span className="grid gap-0.5">
          <strong className="text-sm font-semibold text-on-surface">同时删除本地邮件、草稿、联系人和 Telegram Topic</strong>
          <small className="text-xs font-normal text-on-surface-variant">
            此操作不可恢复；不会删除邮箱服务商服务器中的原始邮件。
          </small>
        </span>
      </label>
      <footer className="mt-1 flex flex-wrap justify-end gap-2">
        <button type="button" className={secondaryButtonClass} data-dialog-initial-focus onClick={onClose}>取消</button>
        <button type="button" className={dangerButtonClass} onClick={() => onConfirm(purgeData)} disabled={deleting}>
          {deleting ? "提交中…" : "确认删除"}
        </button>
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
      const requests = targets.map((id) => {
        const idempotencyKey = deleteKeys.current.get(id) ?? api.idempotencyKey();
        deleteKeys.current.set(id, idempotencyKey);
        return { threadId: id, idempotencyKey };
      });
      const results = targets.length > 1
        ? await api.removeThreads(requests)
        : [{ threadId: targets[0], ...(await api.removeThread(targets[0], requests[0].idempotencyKey)) }];
      const acceptedStatuses = new Set(["accepted", "queued", "running", "deleting", "succeeded", "deleted"]);
      const deletedIds = targets.filter((id) => {
        const result = results.find((item) => item.threadId === id);
        return Boolean(result && acceptedStatuses.has(result.status));
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
  if (authChecking)
    return (
      <main className="grid min-h-screen place-items-center px-4">
        <Loading />
      </main>
    );
  if (authError)
    return (
      <main className="grid min-h-screen place-items-center px-4 py-8">
        <section className={`grid w-full max-w-[400px] gap-3 p-5 ${cardClass}`}>
          <p className="m-0 text-[11px] font-semibold tracking-[0.06em] text-primary-container uppercase">Telegramail</p>
          <h1 className="m-0 text-xl font-semibold tracking-[-0.015em]">无法恢复会话</h1>
          <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
            {authError}
          </p>
          <button className={primaryButtonClass} onClick={() => void bootstrapAuthentication()}>重试</button>
        </section>
      </main>
    );
  if (!ready)
    return (
      <main className="grid min-h-screen place-items-center px-4 py-8">
        <section className={`grid w-full max-w-[400px] gap-3 p-5 ${cardClass}`}>
          <p className="m-0 text-[11px] font-semibold tracking-[0.06em] text-primary-container uppercase">Telegramail</p>
          <h1 className="m-0 text-xl font-semibold tracking-[-0.015em]">绑定邮箱服务</h1>
          <p className="m-0 text-sm leading-relaxed text-on-surface-variant">
            仅首次绑定管理员需要 setup code。绑定后，同一 Telegram 账号在其他设备上会自动登录。
          </p>
          {setupNotice && (
            <p className="m-0 rounded-xl bg-secondary-fixed/50 px-3 py-2.5 text-sm text-on-surface-variant" role="status">
              {setupNotice}
            </p>
          )}
          <form className="grid gap-3" onSubmit={bind}>
            <label className={fieldLabelClass}>
              Setup code
              <input
                className={inputClass}
                autoFocus
                value={setupCode}
                onChange={(e) => setSetupCode(e.target.value.trim())}
              />
            </label>
            {setupError && (
              <p className="m-0 rounded-xl bg-error-container px-3 py-2.5 text-sm font-medium text-on-error-container" role="alert">
                {setupError}
              </p>
            )}
            <button className={primaryButtonClass} disabled={!setupCode}>绑定</button>
          </form>
        </section>
      </main>
    );
  return (
    <>
      <div className="mx-auto w-full max-w-[560px]" ref={appShell}>
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
            <header className="flex flex-wrap items-center justify-between gap-3 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3">
              <div className="grid gap-0.5">
                <p className="m-0 text-[11px] font-semibold tracking-[0.06em] text-primary-container uppercase">Telegramail</p>
                <h1 className="m-0 text-2xl font-bold tracking-[-0.02em]">收件箱</h1>
              </div>
              <div className="flex flex-wrap gap-2">
                <button className={primaryButtonClass} onClick={() => openComposer()} disabled={!activeAccount}>
                  写邮件
                </button>
                <button className={secondaryButtonClass} onClick={() => void openLlmSettings()}>
                  LLM 设置
                </button>
              </div>
            </header>
            <main className="grid gap-4 px-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
              <section className="grid gap-2.5">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="m-0 flex items-center gap-2 text-sm font-semibold">
                    账户
                    {accounts.length > 0 && (
                      <span className="rounded-full bg-surface-container px-2 py-0.5 text-[11px] font-medium text-on-surface-variant">
                        {accounts.length} 个账户
                      </span>
                    )}
                  </h2>
                  <button
                    className={textButtonClass}
                    onClick={() => setAccountModal(false)}
                  >
                    ＋ 添加
                  </button>
                </div>
                {loading ? (
                  <Loading />
                ) : (
                  accounts.map((account) => {
                    const status = account.connectionStatus ?? account.status;
                    return (
                      <article className={`grid grid-cols-[auto_1fr] gap-3 p-3.5 ${cardClass}`} key={account.id}>
                        <div className="flex size-10 items-center justify-center rounded-full bg-primary-fixed text-sm font-semibold text-on-primary-fixed-variant">
                          {initials(account.name)}
                        </div>
                        <div className="grid min-w-0 gap-1.5">
                          <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                            <strong className="text-sm font-semibold break-all">{account.name}</strong>
                            <button
                              className={textButtonClass}
                              onClick={() => setAccountModal(account)}
                            >
                              编辑
                            </button>
                          </div>
                          <small className="text-xs break-all text-on-surface-variant">
                            {account.email} · {account.provider === "custom" ? "IMAP / SMTP" : providerPresets[account.provider as Exclude<ProviderPreset, "custom">]?.label ?? account.provider}
                          </small>
                          <span
                            className={`inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${
                              status === "connected"
                                ? "bg-tertiary-fixed/50 text-tertiary"
                                : status === "failed"
                                  ? "bg-error-container text-on-error-container"
                                  : "bg-surface-container text-on-surface-variant"
                            }`}
                          >
                            <span
                              className={`size-1.5 rounded-full ${
                                status === "connected" ? "bg-tertiary" : status === "failed" ? "bg-error" : "bg-outline"
                              }`}
                            />
                            {status === "checking"
                              ? "正在后台验证连接…"
                              : status === "connected"
                                ? "IMAP / SMTP 已连接"
                                : status === "failed"
                                  ? `连接暂不可用${account.connectionError ? `（${account.connectionError}）` : ""}`
                                  : account.credentialConfigured ? "等待后台验证" : "尚未配置凭据"}
                          </span>
                        </div>
                      </article>
                    );
                  })
                )}
                {!loading && !accounts.length && (
                  <p className={`m-0 p-5 text-center text-sm text-on-surface-variant ${cardClass}`}>尚未添加账户。</p>
                )}
              </section>
              <section className="grid gap-2.5" aria-label="邮件线程">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                  {selectionMode ? (
                    <div className="flex flex-1 items-center gap-2" role="status" aria-live="polite">
                      <strong className="text-sm font-semibold">已选择 {selectedThreadIds.size} 个线程</strong>
                      <button className={textButtonClass} onClick={exitSelection}>取消</button>
                    </div>
                  ) : (
                    <h2 className="m-0 text-sm font-semibold">邮件</h2>
                  )}
                  {!selectionMode && (
                    <small className="text-xs text-on-surface-variant">长按邮件可批量选择</small>
                  )}
                  <span className="flex-1" />
                  {selectionMode && (
                    <button
                      className="inline-flex min-h-9 items-center rounded-lg bg-error-container px-2.5 text-sm font-semibold text-on-error-container transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
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
                  <button className={textButtonClass} onClick={() => void refresh()}>
                    刷新
                  </button>
                </div>
                {loading ? (
                  <Loading />
                ) : groupedThreads.length ? (
                  groupedThreads.map(({ accountId, account, threads: accountThreads }) => (
                    <section className="grid gap-2" key={accountId} aria-labelledby={`account-heading-${accountId}`}>
                      <div className="flex items-center gap-2.5 px-1">
                        <span className="flex size-8 items-center justify-center rounded-full bg-primary-fixed text-xs font-semibold text-on-primary-fixed-variant">
                          {initials(account?.name ?? account?.email ?? "?")}
                        </span>
                        <span className="grid min-w-0 gap-0.5">
                          <strong className="text-xs font-semibold break-all" id={`account-heading-${accountId}`}>
                            {account?.name ?? "其他账户"}
                          </strong>
                          <small className="text-[11px] break-all text-on-surface-variant">
                            {account?.email ?? "未识别账户"} · {accountThreads.length} 封线程
                          </small>
                        </span>
                      </div>
                      <div className={`overflow-hidden ${cardClass}`}>
                      {accountThreads.map((thread) => {
                        const selected = selectedThreadIds.has(thread.id);
                        return (
                          <button
                            className={`flex w-full items-start gap-3 border-b border-outline-variant/40 p-3.5 text-left transition last:border-b-0 active:bg-surface-container-low ${
                              selected ? "bg-primary-fixed/60" : "bg-transparent"
                            }`}
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
                            {selectionMode && (
                              <span
                                className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border text-xs ${
                                  selected
                                    ? "border-primary-container bg-primary-container text-on-primary-container"
                                    : "border-outline-variant bg-surface-container-lowest"
                                }`}
                                aria-hidden="true"
                              >
                                {selected ? "✓" : ""}
                              </span>
                            )}
                            <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary-fixed text-sm font-semibold text-on-primary-fixed-variant">
                              {initials(thread.participants[0] ?? account?.name ?? account?.email)}
                            </span>
                            <span className="grid min-w-0 flex-1 gap-1">
                              <span className="flex items-start justify-between gap-2">
                                <strong className={`text-sm break-words ${thread.unread ? "font-bold" : "font-semibold"}`}>
                                  {thread.subject}
                                </strong>
                                <span className="flex shrink-0 items-center gap-1.5">
                                  {thread.unread && <span className="size-2 rounded-full bg-primary-container" aria-hidden="true" />}
                                  <small className="text-[11px] whitespace-nowrap text-on-surface-variant">{fmtDate(thread.date)}</small>
                                </span>
                              </span>
                              <small className="line-clamp-2 text-xs text-on-surface-variant">
                                {thread.summary ? summaryPlainText(thread.summary) : thread.preview}
                              </small>
                              {thread.summaryStatus && normalizeSummaryStatus(thread.summaryStatus, thread.summary) !== "generated" && (
                                <small
                                  className={`w-fit rounded-md px-1.5 py-0.5 text-[11px] font-medium ${
                                    normalizeSummaryStatus(thread.summaryStatus, thread.summary) === "failed"
                                      ? "bg-error-container text-on-error-container"
                                      : "bg-secondary-fixed text-secondary"
                                  }`}
                                >
                                  {summaryStatusLabel(thread.summaryStatus, thread.summary)}
                                </small>
                              )}
                            </span>
                          </button>
                        );
                      })}
                      </div>
                    </section>
                  ))
                ) : (
                  <p className={`m-0 p-5 text-center text-sm text-on-surface-variant ${cardClass}`}>没有邮件。</p>
                )}
              </section>
            </main>
          </>
        )}
      </div>
      {accountModal !== null && (
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-inverse-surface/40 p-3 backdrop-blur-[3px] sm:items-center">
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
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-inverse-surface/40 p-3 backdrop-blur-[3px] sm:items-center">
          <LlmSettingsForm
            settings={llmSettings}
            onClose={() => setLlmSettingsOpen(false)}
            toast={setToast}
            onSave={(saved) => setLlmSettings(saved)}
          />
        </div>
      )}
      {compose && composeAccount && (
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-inverse-surface/40 p-3 backdrop-blur-[3px] sm:items-center">
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
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-inverse-surface/40 p-3 backdrop-blur-[3px] sm:items-center">
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
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-inverse-surface/40 p-3 backdrop-blur-[3px] sm:items-center">
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

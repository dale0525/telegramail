import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    constructor(message: string, readonly status: number) {
      super(message);
    }
  }
  return {
    MockApiError,
    apiMock: {
      authStatus: vi.fn(),
      session: vi.fn(),
      accounts: vi.fn(),
      threads: vi.fn(),
      thread: vi.fn(),
      contacts: vi.fn(),
      saveAccount: vi.fn(),
      removeAccount: vi.fn(),
      send: vi.fn(),
      removeThread: vi.fn(),
      removeThreads: vi.fn(),
      llmSettings: vi.fn(),
      saveLlmSettings: vi.fn(),
      testLlmConnection: vi.fn(),
      retrySummary: vi.fn(),
      idempotencyKey: vi.fn(() => "stable-idempotency-key"),
    },
  };
});

vi.mock("./api", () => ({ api: apiMock, ApiError: MockApiError }));

import {
  AccountForm,
  AccountDeleteConfirmation,
  App,
  accountIsUsable,
  Composer,
  clearComposeDraft,
  composeDraftStorageKey,
  COMPOSE_DRAFT_STORAGE_KEY,
  DeleteConfirmation,
  getMiniAppRoute,
  getTelegramInitData,
  prefixedSubject,
  replyAllRecipients,
  replyRecipients,
  loadComposeDraft,
  loadRecentAccountId,
  RECENT_ACCOUNT_STORAGE_KEY,
  recentAccountStorageKey,
  groupThreadsByAccount,
  hasExternalEmailImages,
  ThreadDetail,
  sanitizeEmailHtml,
  sanitizeSummaryHtml,
  appendSignature,
  parseAccountSignatures,
  removeTrailingSignature,
  replaceTrailingSignature,
  resolveAccountSignature,
  serializeAccountSignatures,
  saveComposeDraft,
  saveRecentAccountId,
  TokenInput,
} from "./main";
import { providerPresets } from "./providers";

const account = {
  id: "1",
  email: "me@example.test",
  name: "测试账户",
  provider: "IMAP",
  status: "connected" as const,
  credentialConfigured: true,
  imapServer: "imap.example.test",
  imapPort: 993,
  imapSsl: true,
  smtpServer: "smtp.example.test",
  smtpPort: 465,
  smtpSsl: true,
  enabled: true,
};
const testTelegramUserId = 42;

const legacySignature = JSON.stringify({
  version: 1,
  default: "second",
  items: [
    { id: "first", markdown: "First signature" },
    { id: "second", markdown: "Selected signature" },
  ],
});

describe("Mini App accessibility and destructive action safeguards", () => {
  it("parses Telegram Mini App deep-link routes", () => {
    const original = window.location.href;
    window.history.replaceState({}, "", "/?action=reply&thread_id=42");
    expect(getMiniAppRoute()).toEqual({ action: "reply", threadId: "42" });
    window.history.replaceState({}, "", original);
  });

  it("restores the v1 common mail provider presets", () => {
    expect(Object.keys(providerPresets)).toEqual([
      "gmail", "outlook", "microsoft365", "yahoo", "icloud", "zoho", "aol", "gmx",
      "qq", "netease", "exmail", "alimail", "yandex", "linuxdo",
    ]);
    expect(providerPresets.netease.smtpServer).toBe("smtp.163.com");
    expect(providerPresets.exmail.imapServer).toBe("imap.exmail.qq.com");
    expect(providerPresets.icloud.smtpSsl).toBe(false);
  });

  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(window, { Telegram: undefined });
    const values = new Map<string, string>();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: vi.fn((key: string) => values.get(key) ?? null),
        setItem: vi.fn((key: string, value: string) => values.set(key, value)),
        removeItem: vi.fn((key: string) => values.delete(key)),
      },
    });
  });

  afterEach(cleanup);

  it("moves focus into the account dialog and closes it with Escape", async () => {
    const onClose = vi.fn();
    render(<AccountForm onClose={onClose} onSave={vi.fn()} toast={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "添加账户" });
    await waitFor(() => expect(screen.getByLabelText("显示名称")).toHaveFocus());
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("manages signature previews, defaults, and clearing an account signature", async () => {
    const saved = { ...account, signature: null };
    apiMock.saveAccount.mockResolvedValue(saved);
    const onSave = vi.fn();
    render(<AccountForm account={{ ...account, signature: null }} onClose={vi.fn()} onSave={onSave} toast={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("新签名名称"), { target: { value: "工作" } });
    fireEvent.change(screen.getByLabelText("新签名内容"), { target: { value: "**姓名**\n\n团队" } });
    fireEvent.click(screen.getByRole("button", { name: "添加签名" }));
    expect(screen.getByLabelText("签名预览 工作")).toHaveTextContent("姓名");
    expect(screen.getByLabelText("签名预览 工作").querySelector("strong")).not.toBeNull();

    fireEvent.change(screen.getByLabelText("新签名名称"), { target: { value: "个人" } });
    fireEvent.change(screen.getByLabelText("新签名内容"), { target: { value: "*个人签名*" } });
    fireEvent.click(screen.getByRole("button", { name: "添加签名" }));
    fireEvent.click(screen.getByRole("button", { name: "设为默认" }));
    expect(screen.getByLabelText("签名预览 个人").querySelector("em")).not.toBeNull();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "保存账户" }));
    await waitFor(() => expect(apiMock.saveAccount).toHaveBeenCalledWith(expect.objectContaining({ signature: null })));
    expect(onSave).toHaveBeenCalledWith(saved);
  });

  it("confirms account deletion with an explicit optional purge checkbox", () => {
    const onConfirm = vi.fn();
    render(<AccountDeleteConfirmation account={account} onClose={vi.fn()} onConfirm={onConfirm} />);
    const checkbox = screen.getByRole("checkbox", { name: /同时删除本地邮件/ });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  it("chooses the sender account inside a new compose window", async () => {
    const second = { ...account, id: "2", email: "second@example.test", name: "第二账户", signature: "**Second**" };
    apiMock.send.mockResolvedValue({ status: "sent", id: "sent-operation" });
    render(<Composer account={account} accounts={[account, second]} initial={{ to: ["to@example.test"] }} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    fireEvent.change(screen.getByLabelText("发件账户"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    await waitFor(() => expect(apiMock.send).toHaveBeenCalledWith(expect.objectContaining({ accountId: "2" }), expect.any(String)));
  });

  it("shows BCC recipients and attachment details before sending", () => {
    render(
      <Composer
        account={account}
        initial={{
          to: ["to@example.test"],
          bcc: ["hidden@example.test"],
          subject: "Preview",
          attachments: [{ name: "report.pdf", size: 2048 }],
        }}
        onClose={vi.fn()}
        toast={vi.fn()}
        telegramUserId={testTelegramUserId}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(screen.getByText("密送：")).toBeInTheDocument();
    expect(screen.getByText("hidden@example.test")).toBeInTheDocument();
    expect(screen.getByText(/report\.pdf/)).toBeInTheDocument();
  });

  it("does not offer failed accounts as a sender or recent-account fallback", () => {
    const failed = { ...account, id: "failed", email: "failed@example.test", connectionStatus: "failed" };
    render(<Composer account={account} accounts={[failed, account]} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    expect(screen.queryByRole("option", { name: /failed@example.test/ })).toBeNull();
    expect(screen.getByRole("option", { name: /测试账户/ })).toBeInTheDocument();
    expect(accountIsUsable(failed)).toBe(false);
    expect(accountIsUsable(account)).toBe(true);
  });

  it("namespaces the recent sender account by Telegram user", () => {
    saveRecentAccountId(42, "account-42");
    saveRecentAccountId(43, "account-43");
    expect(recentAccountStorageKey(42)).toBe(`${RECENT_ACCOUNT_STORAGE_KEY}.user.42`);
    expect(loadRecentAccountId(42)).toBe("account-42");
    expect(loadRecentAccountId(43)).toBe("account-43");
    expect(loadRecentAccountId(null)).toBeNull();
  });

  it("uses a combobox with active descendant and selectable listbox options", async () => {
    apiMock.contacts.mockResolvedValue([{ name: "Alice", email: "alice@example.test" }]);
    const onChange = vi.fn();
    render(<TokenInput label="收件人" values={[]} onChange={onChange} />);

    const input = screen.getByRole("combobox", { name: "收件人" });
    fireEvent.change(input, { target: { value: "al" } });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 170));
    });

    const option = await screen.findByRole("option", { name: /Alice/ });
    expect(input).toHaveAttribute("aria-controls");
    expect(option.querySelector("button")).toBeNull();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input).toHaveAttribute("aria-activedescendant", option.id);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(["alice@example.test"]);
  });

  it("groups inbox threads by account and keeps their order", () => {
    const second = { ...account, id: "2", email: "second@example.test", name: "第二账户" };
    const threads = [
      { id: "a", accountId: "1", subject: "A", preview: "", participants: [], date: "2026-01-01", messages: [] },
      { id: "b", accountId: "2", subject: "B", preview: "", participants: [], date: "2026-01-02", messages: [] },
      { id: "c", accountId: "1", subject: "C", preview: "", participants: [], date: "2026-01-03", messages: [] },
    ];
    expect(groupThreadsByAccount(threads, [account, second]).map((group) => [group.accountId, group.threads.map((item) => item.id)])).toEqual([
      ["1", ["a", "c"]],
      ["2", ["b"]],
    ]);
  });

  it("sanitizes untrusted email HTML while preserving formatting and safe links", () => {
    const html = sanitizeEmailHtml('<p onclick="alert(1)"><strong>Bold</strong><script>alert(2)</script> <a href="javascript:alert(3)">bad</a> <a href="https://example.test">good</a></p>');
    expect(html).toContain("<strong>Bold</strong>");
    expect(html).toContain('href="https://example.test/"');
    expect(html).not.toContain("script");
    expect(html).not.toContain("onclick");
    expect(html).not.toContain("javascript:");
  });

  it("keeps only hexadecimal header background colors from email styles", () => {
    const html = sanitizeEmailHtml('<div style="background:#d9272e; background-image:url(https://tracker.example/x); color:#fff"><img src="cid:logo"></div>');
    expect(html).toContain("background-color: #d9272e");
    expect(html).not.toContain("background-image");
    expect(html).not.toContain("tracker.example");
  });

  it("does not load external images until explicitly allowed", () => {
    const source = '<p><img src="https://images.example.test/pixel.png" alt="promo"></p>';
    const blocked = sanitizeEmailHtml(source);
    expect(blocked).toContain('data-telegramail-external-src="https://images.example.test/pixel.png"');
    expect(blocked).not.toMatch(/<img[^>]+\ssrc=/);
    expect(hasExternalEmailImages(source)).toBe(true);

    const allowed = sanitizeEmailHtml(source, { allowExternalImages: true });
    expect(allowed).toContain('src="https://images.example.test/pixel.png"');
    expect(allowed).not.toContain("data-telegramail-external-src");
  });

  it("upgrades legacy HTTP and lazy image sources after explicit opt-in", () => {
    const source = '<img src="http://images.example.test/pixel.png" data-src="https://images.example.test/large.png" srcset="https://images.example.test/large.png 2x">';
    const blocked = sanitizeEmailHtml(source);
    expect(blocked).toContain('data-telegramail-external-src="https://images.example.test/pixel.png"');
    expect(blocked).not.toContain("http://");

    const allowed = sanitizeEmailHtml(source, { allowExternalImages: true });
    expect(allowed).toContain('src="https://images.example.test/pixel.png"');
    expect(allowed).not.toContain("data-src");
    expect(allowed).not.toContain("srcset");
  });

  it("sanitizes descendants before unwrapping unknown email containers", () => {
    const html = sanitizeEmailHtml(
      '<custom><img src="https://images.example.test/a.png" onerror="alert(1)"><a href="javascript:alert(2)">bad</a><script>alert(3)</script></custom>',
    );
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("<script");
    expect(html).toContain("data-telegramail-external-src");
  });

  it("resolves only mapped cid images to authenticated local asset URLs", () => {
    const source = '<p><img src="cid:%3Clogo%40example.test%3E" alt="logo"><img src="cid:missing@example.test"></p>';
    const html = sanitizeEmailHtml(source, {
      inlineImageUrls: { "<logo@example.test>": "/api/v1/emails/12/inline-assets/7" },
    });
    expect(html).toContain('src="/api/v1/emails/12/inline-assets/7"');
    expect(html).not.toContain("cid:");
    expect(html).not.toContain("missing@example.test");
    expect(hasExternalEmailImages(source)).toBe(false);
    expect(sanitizeEmailHtml('<img src="cid:logo@example.test">', {
      inlineImageUrls: { "logo@example.test": "https://evil.example.test/x.png" },
    })).not.toContain("https://evil.example.test");
  });

  it("renders CID images without showing the external-image opt-in", () => {
    const thread = {
      id: "cid-thread",
      accountId: account.id,
      subject: "内嵌图片",
      preview: "",
      participants: ["sender@example.test"],
      date: "2026-01-01T00:00:00.000Z",
      messages: [{
        id: "12",
        accountId: account.id,
        from: "sender@example.test",
        to: [account.email],
        date: "2026-01-01T00:00:00.000Z",
        body: "",
        html: '<p><img src="cid:logo@example.test" alt="logo"></p>',
        inlineAssets: [{ id: "7", contentId: "logo@example.test", mimeType: "image/png", size: 4 }],
      }],
    };
    const view = render(
      <ThreadDetail
        thread={thread}
        account={account}
        onBack={vi.fn()}
        onOpenLlmSettings={vi.fn()}
        onCompose={vi.fn()}
        onDelete={vi.fn()}
        onRetrySummary={vi.fn()}
        canDelete
      />,
    );
    expect(screen.queryByRole("button", { name: "显示外部图片" })).toBeNull();
    expect(view.container.querySelector('img[src="/api/v1/emails/12/inline-assets/7"]')).not.toBeNull();
  });

  it("offers an explicit per-thread action before loading remote email images", () => {
    const thread = {
      id: "image-thread",
      accountId: account.id,
      subject: "外部图片",
      preview: "",
      participants: ["sender@example.test"],
      date: "2026-01-01T00:00:00.000Z",
      messages: [{
        id: "image-message",
        accountId: account.id,
        from: "sender@example.test",
        to: [account.email],
        date: "2026-01-01T00:00:00.000Z",
        body: "",
        html: '<p><img src="https://images.example.test/banner.png" alt="banner"></p>',
      }],
    };
    const view = render(
      <ThreadDetail
        thread={thread}
        account={account}
        onBack={vi.fn()}
        onOpenLlmSettings={vi.fn()}
        onCompose={vi.fn()}
        onDelete={vi.fn()}
        onRetrySummary={vi.fn()}
        canDelete
      />,
    );

    expect(screen.getByRole("button", { name: "显示外部图片" })).toBeInTheDocument();
    expect(view.container.querySelector("img[src]" )).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "显示外部图片" }));
    expect(view.container.querySelector('img[src="https://images.example.test/banner.png"]')).not.toBeNull();
  });

  it("keeps the external image action in the body view", () => {
    const thread = {
      id: "summary-image-thread",
      accountId: account.id,
      subject: "摘要与外部图片",
      preview: "摘要",
      participants: ["sender@example.test"],
      date: "2026-01-01T00:00:00.000Z",
      summary: "摘要",
      summaryStatus: "generated" as const,
      messages: [{
        id: "summary-image-message",
        accountId: account.id,
        from: "sender@example.test",
        to: [account.email],
        date: "2026-01-01T00:00:00.000Z",
        body: "",
        html: '<p><img src="https://images.example.test/banner.png" alt="banner"></p>',
        summary: "摘要",
        summaryStatus: "generated" as const,
      }],
    };
    render(
      <ThreadDetail
        thread={thread}
        account={account}
        onBack={vi.fn()}
        onOpenLlmSettings={vi.fn()}
        onCompose={vi.fn()}
        onDelete={vi.fn()}
        onRetrySummary={vi.fn()}
        canDelete
      />,
    );

    expect(screen.queryByRole("button", { name: "显示外部图片" })).toBeNull();
    expect(screen.getByRole("tab", { name: "摘要" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "正文" }));
    expect(screen.getByRole("button", { name: "显示外部图片" })).toBeInTheDocument();
  });

  it("dismisses contact suggestions with Escape without closing the composer", async () => {
    apiMock.contacts.mockResolvedValue([{ name: "Alice", email: "alice@example.test" }]);
    const onClose = vi.fn();
    render(<Composer account={account} onClose={onClose} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    const input = screen.getByRole("combobox", { name: "收件人" });
    fireEvent.change(input, { target: { value: "al" } });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 170));
    });
    await screen.findByRole("option", { name: /Alice/ });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "联系人建议" })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("reuses the idempotency key for a failed compose send retry", async () => {
    apiMock.send.mockRejectedValue(new Error("发送失败"));
    render(
      <Composer
        account={account}
        initial={{ to: ["alice@example.test"], subject: "重试" }}
        onClose={vi.fn()}
        toast={vi.fn()}
        telegramUserId={testTelegramUserId}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    await screen.findByRole("button", { name: "重试发送" });
    fireEvent.click(screen.getByRole("button", { name: "重试发送" }));
    await waitFor(() => expect(apiMock.send).toHaveBeenCalledTimes(2));
    expect(apiMock.send.mock.calls[0][1]).toBe("stable-idempotency-key");
    expect(apiMock.send.mock.calls[1][1]).toBe("stable-idempotency-key");
  });

  it("announces destructive provider and Topic impact before confirmation", () => {
    const onConfirm = vi.fn();
    render(
      <DeleteConfirmation provider="IMAP" onClose={vi.fn()} onConfirm={onConfirm} />,
    );

    expect(screen.getByRole("dialog", { name: "确认删除线程？" })).toHaveAccessibleDescription(/Telegram Topic/);
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("returns to the full inbox as soon as a Topic deletion is queued", async () => {
    const thread = {
      id: "thread-1",
      subject: "待删除邮件",
      preview: "正文预览",
      participants: ["sender@example.test"],
      date: new Date().toISOString(),
      accountId: account.id,
      messages: [{
        id: "message-1",
        from: "sender@example.test",
        to: [account.email],
        date: new Date().toISOString(),
        body: "正文",
      }],
    };
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([{ ...account, provider: "gmail" }]);
    apiMock.threads.mockResolvedValue([thread]);
    apiMock.thread.mockResolvedValue(thread);
    apiMock.removeThread.mockResolvedValue({ id: "delete:1", status: "queued" });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /待删除邮件/ }));
    expect(await screen.findByRole("heading", { name: "待删除邮件" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("heading", { name: "收件箱" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "待删除邮件" })).toBeNull();
    expect(apiMock.removeThread).toHaveBeenCalledWith("thread-1", "stable-idempotency-key");
    expect(await screen.findByRole("status")).toHaveTextContent("正在后台同步邮箱与 Telegram Topic");
  });

  it("keeps a thread visible when the server reports a failed delete operation", async () => {
    const thread = {
      id: "thread-failed",
      subject: "删除失败邮件",
      preview: "正文预览",
      participants: ["sender@example.test"],
      date: new Date().toISOString(),
      accountId: account.id,
      messages: [],
    };
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([account]);
    apiMock.threads.mockResolvedValue([thread]);
    apiMock.thread.mockResolvedValue(thread);
    apiMock.removeThread.mockResolvedValue({ id: "delete:failed", status: "failed" });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /删除失败邮件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("heading", { name: "删除失败邮件" })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("1 个线程失败");
  });

  it("enters selection mode on long press and submits a batch delete", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    const secondAccount = { ...account, id: "2", email: "second@example.test", name: "第二账户", provider: "qq" };
    const threads = [
      {
        id: "thread-1",
        subject: "第一线程",
        preview: "第一封邮件",
        participants: ["one@example.test"],
        date: new Date().toISOString(),
        accountId: account.id,
        messages: [],
      },
      {
        id: "thread-2",
        subject: "第二线程",
        preview: "第二封邮件",
        participants: ["two@example.test"],
        date: new Date().toISOString(),
        accountId: secondAccount.id,
        messages: [],
      },
    ];
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([account, secondAccount]);
    apiMock.threads.mockResolvedValue(threads);
    apiMock.removeThreads.mockResolvedValue([
      { threadId: "thread-1", id: "delete:1", status: "queued" },
      { threadId: "thread-2", id: "delete:2", status: "queued" },
    ]);

    render(<App />);
    const first = await screen.findByRole("button", { name: /第一线程/ });
    fireEvent.pointerDown(first, { clientX: 10, clientY: 10 });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 560));
    });
    fireEvent.pointerUp(first);
    // A real pointer sequence emits a click after pointerup; the component
    // consumes that click so a long press does not open the thread.
    fireEvent.click(first);
    expect(await screen.findByText("已选择 1 个线程")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: /第二线程/ }));
    expect(screen.getByText("已选择 2 个线程")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(await screen.findByRole("heading", { name: "确认删除 2 个线程？" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(apiMock.removeThreads).toHaveBeenCalledTimes(1));
    expect(apiMock.removeThreads).toHaveBeenCalledWith([
      { threadId: "thread-1", idempotencyKey: "stable-idempotency-key" },
      { threadId: "thread-2", idempotencyKey: "stable-idempotency-key" },
    ]);
    expect(apiMock.removeThread).not.toHaveBeenCalled();
    expect(await screen.findByText("没有邮件。")).toBeInTheDocument();
  });

  it("enters selection mode from a touch long press", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    const thread = {
      id: "touch-thread",
      subject: "触摸长按邮件",
      preview: "正文预览",
      participants: ["touch@example.test"],
      date: new Date().toISOString(),
      accountId: account.id,
      messages: [],
    };
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([account]);
    apiMock.threads.mockResolvedValue([thread]);

    render(<App />);
    const first = await screen.findByRole("button", { name: /触摸长按邮件/ });
    fireEvent.touchStart(first, { touches: [{ clientX: 10, clientY: 10 }] });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 560));
    });
    fireEvent.touchEnd(first);

    expect(await screen.findByText("已选择 1 个线程")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
  });

  it("reads signed Telegram launch data from the Web fragment before the SDK is ready", () => {
    window.history.replaceState({}, "", "/#tgWebAppData=query_id%3Dsafe%26hash%3Dsigned");
    expect(getTelegramInitData()).toBe("query_id=safe&hash=signed");
    window.history.replaceState({}, "", "/");
  });

  it("uses the selected legacy signature body instead of exposing its JSON envelope", () => {
    expect(resolveAccountSignature(legacySignature)).toBe("Selected signature");
    expect(resolveAccountSignature("Plain signature")).toBe("Plain signature");
  });

  it("prefills compose with the selected legacy signature body", () => {
    render(
      <Composer
        account={{ ...account, signature: legacySignature }}
        onClose={vi.fn()}
        toast={vi.fn()}
        telegramUserId={testTelegramUserId}
      />,
    );
    expect(screen.getByLabelText("正文")).toHaveValue("Selected signature");
  });

  it("round-trips named signatures and keeps the selected default", () => {
    const signatures = parseAccountSignatures(legacySignature);
    expect(signatures.defaultId).toBe("second");
    expect(serializeAccountSignatures(signatures)).toContain('"default":"second"');
    expect(parseAccountSignatures(null)).toEqual({ items: [], defaultId: null });
    expect(serializeAccountSignatures({ items: [], defaultId: null })).toBeNull();
  });

  it("appends, replaces, and removes only a trailing inserted signature", () => {
    expect(appendSignature("正文", "**签名**")).toBe("正文\n\n**签名**");
    expect(replaceTrailingSignature("正文\n\n**旧**", "**旧**", "*新*"))
      .toBe("正文\n\n*新*");
    expect(removeTrailingSignature("正文\n\n**签名**", "**签名**")).toBe("正文");
    expect(removeTrailingSignature("正文中有 **签名**", "**签名**")).toBe("正文中有 **签名**");
  });

  it("previews and switches compose signatures without losing the body", () => {
    const signatures = JSON.stringify({
      version: 1,
      default: "work",
      items: [
        { id: "work", name: "工作", markdown: "**工作签名**" },
        { id: "personal", name: "个人", markdown: "*个人签名*" },
      ],
    });
    const sender = { ...account, signature: signatures };
    render(<Composer account={sender} initial={{ to: ["to@example.test"], body: "正文" }} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);

    expect(screen.getByRole("combobox", { name: "签名" })).toHaveValue("work");
    fireEvent.click(screen.getByRole("button", { name: "插入签名" }));
    expect(screen.getByLabelText("正文")).toHaveValue("正文\n\n**工作签名**");
    fireEvent.change(screen.getByRole("combobox", { name: "签名" }), { target: { value: "personal" } });
    expect(screen.getByLabelText("正文")).toHaveValue("正文\n\n*个人签名*");
    fireEvent.click(screen.getByRole("button", { name: "删除签名" }));
    expect(screen.getByLabelText("正文")).toHaveValue("正文");
    fireEvent.change(screen.getByLabelText("正文"), { target: { value: "正文\n\n手写内容" } });
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(screen.getByText("手写内容")).toBeInTheDocument();
    expect(screen.queryByText("工作签名")).not.toBeInTheDocument();
  });

  it("renders Markdown semantics, including tables, in the compose preview", () => {
    render(<Composer account={account} initial={{ to: ["to@example.test"], body: "# 标题\n\n~~删除~~\n\n| A | B |\n|---|---|\n| 1 | 2 |" }} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(document.querySelector(".preview-body s")).toHaveTextContent("删除");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("renders safe Markdown in the send preview", () => {
    render(
      <Composer
        account={account}
        initial={{
          to: ["to@example.test"],
          subject: "Preview",
          body: "**Bold**\n\n*Italic*\n\n- Item\n\n[Link](https://example.test)\n\n<script>alert(1)</script>",
        }}
        onClose={vi.fn()}
        toast={vi.fn()}
        telegramUserId={testTelegramUserId}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(screen.getByText("Bold").tagName).toBe("STRONG");
    expect(screen.getByText("Italic").tagName).toBe("EM");
    expect(screen.getByRole("link", { name: "Link" })).toHaveAttribute("href", "https://example.test/");
    expect(screen.queryByText("alert(1)", { selector: "script" })).not.toBeInTheDocument();
  });

  it("does not stack reply and forward prefixes", () => {
    expect(prefixedSubject("Re: Existing", "Re")).toBe("Re: Existing");
    expect(prefixedSubject("re: Existing", "Re")).toBe("re: Existing");
    expect(prefixedSubject("Existing", "Re")).toBe("Re: Existing");
    expect(prefixedSubject("Fwd: Existing", "Fwd")).toBe("Fwd: Existing");
  });

  it("builds reply-all recipients without replying to the current account", () => {
    const message = {
      id: "m1",
      from: "Logic Tan <logictan89@gmail.com>",
      to: ["812388447@qq.com", "other@example.test"],
      cc: ["Other <OTHER@example.test>", "copy@example.test"],
      date: new Date().toISOString(),
      body: "hello",
    };
    expect(replyRecipients(message, "812388447@qq.com", false)).toEqual([
      "logictan89@gmail.com",
    ]);
    expect(replyAllRecipients(message, "812388447@qq.com")).toEqual({
      to: ["logictan89@gmail.com", "other@example.test"],
      cc: ["copy@example.test"],
    });
  });

  it("automatically restores a session for the same bound Telegram user", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    apiMock.authStatus.mockResolvedValue({ authenticated: false });
    apiMock.session.mockResolvedValue({ authenticated: true });
    apiMock.accounts.mockResolvedValue([]);
    apiMock.threads.mockResolvedValue([]);

    render(<App />);

    await waitFor(() => expect(apiMock.session).toHaveBeenCalledWith("signed-init-data"));
    expect(await screen.findByRole("heading", { name: "收件箱" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Setup code")).not.toBeInTheDocument();
  });

  it("falls back to setup code when session restoration is rejected for an unbound user", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "unbound-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    apiMock.authStatus.mockResolvedValue({ authenticated: false });
    apiMock.session.mockRejectedValue(new MockApiError("Not authorized", 403));

    render(<App />);

    expect(await screen.findByRole("heading", { name: "绑定邮箱服务" })).toBeInTheDocument();
    expect(screen.getByText(/仅首次绑定需要 setup code/)).toBeInTheDocument();
  });

  it("shows a retry action after an authentication request failure", async () => {
    apiMock.authStatus.mockRejectedValueOnce(new Error("网络不可用")).mockResolvedValueOnce({ authenticated: true });
    apiMock.accounts.mockResolvedValue([]);
    apiMock.threads.mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("网络不可用");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "收件箱" })).toBeInTheDocument();
  });

  it("saves a text draft and restores recipients, content, and the selected account on reopen", async () => {
    const onClose = vi.fn();
    render(
      <Composer
        account={account}
        initial={{
          to: ["to@example.test"],
          cc: ["cc@example.test"],
          bcc: ["bcc@example.test"],
          subject: "保存的主题",
          body: "保存的正文",
        }}
        onClose={onClose}
        toast={vi.fn()}
        telegramUserId={testTelegramUserId}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存草稿并关闭" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(loadComposeDraft(testTelegramUserId)).toEqual({
      activeAccountId: "1",
      to: ["to@example.test"],
      cc: ["cc@example.test"],
      bcc: ["bcc@example.test"],
      subject: "保存的主题",
      body: "保存的正文",
    });

    cleanup();
    render(<Composer account={account} initial={loadComposeDraft(testTelegramUserId) ?? undefined} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    expect(screen.getByText("to@example.test")).toBeInTheDocument();
    expect(screen.getByLabelText("主题")).toHaveValue("保存的主题");
    expect(screen.getByLabelText("正文")).toHaveValue("保存的正文");
  });

  it("opens a new compose with the persisted draft and its active account", async () => {
    saveComposeDraft(testTelegramUserId, { activeAccountId: "1", to: ["to@example.test"], cc: [], bcc: [], subject: "自动恢复", body: "自动恢复正文" });
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([{ ...account, provider: "gmail" }]);
    apiMock.threads.mockResolvedValue([]);
    render(<App />);

    const composeButton = await screen.findByRole("button", { name: "写邮件" });
    await waitFor(() => expect(composeButton).toBeEnabled());
    fireEvent.click(composeButton);
    expect(await screen.findByLabelText("主题")).toHaveValue("自动恢复");
    expect(screen.getByLabelText("正文")).toHaveValue("自动恢复正文");
    expect(screen.getByText("to@example.test")).toBeInTheDocument();
  });

  it("clears the stored draft only after a successful send", async () => {
    saveComposeDraft(testTelegramUserId, { activeAccountId: "1", to: ["to@example.test"], cc: [], bcc: [], subject: "待发送", body: "正文" });
    apiMock.send.mockResolvedValue({ status: "sent", id: "sent-operation" });
    render(<Composer account={account} initial={loadComposeDraft(testTelegramUserId) ?? undefined} onClose={vi.fn()} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    await waitFor(() => expect(apiMock.send).toHaveBeenCalledTimes(1));
    expect(loadComposeDraft(testTelegramUserId)).toBeNull();
  });

  it("saves the latest queued message before closing the composer", async () => {
    const onClose = vi.fn();
    apiMock.send.mockResolvedValue({ status: "queued", id: "queued-operation" });
    render(<Composer account={account} initial={{ to: ["to@example.test"] }} onClose={onClose} toast={vi.fn()} telegramUserId={testTelegramUserId} />);
    fireEvent.change(screen.getByLabelText("主题"), { target: { value: "排队后的最新主题" } });
    fireEvent.change(screen.getByLabelText("正文"), { target: { value: "排队后的最新正文" } });
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    await waitFor(() => expect(apiMock.send).toHaveBeenCalledTimes(1));
    expect(loadComposeDraft(testTelegramUserId)).toMatchObject({ subject: "排队后的最新主题", body: "排队后的最新正文" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1), { timeout: 1000 });
  });

  it("isolates drafts by authenticated Telegram user and never reads drafts for an unknown identity", () => {
    saveComposeDraft(42, { activeAccountId: "1", to: ["first@example.test"], cc: [], bcc: [], subject: "用户一", body: "内容一" });
    expect(loadComposeDraft(43)).toBeNull();
    expect(loadComposeDraft(42)).toMatchObject({ subject: "用户一" });
    vi.clearAllMocks();
    expect(loadComposeDraft(null)).toBeNull();
    expect(window.localStorage.getItem).not.toHaveBeenCalled();
  });

  it("keeps a successful send successful when clearing local storage throws", async () => {
    saveComposeDraft(testTelegramUserId, { activeAccountId: "1", to: ["to@example.test"], cc: [], bcc: [], subject: "已发送", body: "正文" });
    const toast = vi.fn();
    apiMock.send.mockResolvedValue({ status: "sent", id: "sent-operation" });
    window.localStorage.removeItem = vi.fn(() => { throw new Error("storage unavailable"); });
    render(<Composer account={account} initial={loadComposeDraft(testTelegramUserId) ?? undefined} onClose={vi.fn()} toast={toast} telegramUserId={testTelegramUserId} />);
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    await waitFor(() => expect(apiMock.send).toHaveBeenCalledTimes(1));
    expect(toast).toHaveBeenCalledWith("邮件已发送。");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores and removes corrupted stored drafts safely", () => {
    const key = composeDraftStorageKey(testTelegramUserId)!;
    window.localStorage.setItem(key, "{not-json");
    expect(loadComposeDraft(testTelegramUserId)).toBeNull();
    expect(window.localStorage.removeItem).toHaveBeenCalledWith(key);
    clearComposeDraft(testTelegramUserId);
  });

  it("warns that attachments are not saved and excludes them from the local draft", () => {
    const toast = vi.fn();
    render(
      <Composer
        account={account}
        initial={{ attachments: [{ name: "report.pdf", size: 12, file: new File(["x"], "report.pdf") }] }}
        onClose={vi.fn()}
        toast={toast}
        telegramUserId={testTelegramUserId}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存草稿并关闭" }));
    expect(toast).toHaveBeenCalledWith("草稿已保存；附件需重新添加。");
    expect(window.localStorage.getItem(composeDraftStorageKey(testTelegramUserId)!)).not.toContain("report.pdf");
  });

  it("opens global LLM settings, tests the connection, and saves without exposing the key", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([account]);
    apiMock.threads.mockResolvedValue([]);
    apiMock.llmSettings.mockResolvedValue({
      enabled: false,
      baseUrl: "https://llm.example.test/v1",
      model: "small-model",
      defaultLanguage: "en_US",
      apiKeyConfigured: true,
      summaryThreshold: 400,
      lastTestStatus: "never",
      failedCount: 2,
    });
    apiMock.testLlmConnection.mockResolvedValue({ ok: true, last_test_status: "ok" });
    apiMock.saveLlmSettings.mockResolvedValue({
      enabled: true,
      baseUrl: "https://llm.example.test/v1",
      model: "small-model",
      defaultLanguage: "zh_CN",
      apiKeyConfigured: true,
      summaryThreshold: 400,
      lastTestStatus: "ok",
      failedCount: 2,
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "LLM 设置" }));
    expect(await screen.findByRole("dialog", { name: "LLM 设置" })).toBeInTheDocument();
    expect(screen.getByLabelText("Base URL")).toHaveValue("https://llm.example.test/v1");
    fireEvent.change(screen.getByLabelText("摘要语言"), { target: { value: "zh_CN" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(apiMock.testLlmConnection).toHaveBeenCalledWith(expect.objectContaining({ baseUrl: "https://llm.example.test/v1" })));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(apiMock.saveLlmSettings).toHaveBeenCalledWith(expect.objectContaining({ model: "small-model", defaultLanguage: "zh_CN", summaryThreshold: 400 })));
  });

  it("renders only the supported LLM summary HTML tags", () => {
    const rendered = sanitizeSummaryHtml("<b>重点</b><i onclick='bad'>说明</i><a href='https://evil.invalid'>链接</a><script>alert(1)</script>");
    expect(rendered).toContain("<b>重点</b>");
    expect(rendered).toContain("<i>说明</i>");
    expect(rendered).toContain("链接");
    expect(rendered).not.toContain("onclick");
    expect(rendered).not.toContain("href");
    expect(rendered).not.toContain("<script>");
  });

  it("prefers a generated summary in the inbox and retries a failed detail summary", async () => {
    Object.assign(window, {
      Telegram: { WebApp: { initData: "signed-init-data", ready: vi.fn(), expand: vi.fn(), onEvent: vi.fn() } },
    });
    const thread = {
      id: "summary-thread",
      subject: "摘要主题",
      preview: "正文预览",
      summary: "服务端生成的摘要",
      summaryStatus: "failed",
      participants: ["sender@example.test"],
      date: new Date().toISOString(),
      accountId: account.id,
      messages: [{ id: "email-42", from: "sender@example.test", to: [account.email], date: new Date().toISOString(), body: "很长的正文", summary: "服务端生成的摘要", summaryStatus: "failed" }],
    };
    apiMock.authStatus.mockResolvedValue({ authenticated: true, telegram_user_id: testTelegramUserId });
    apiMock.accounts.mockResolvedValue([account]);
    apiMock.threads.mockResolvedValue([thread]);
    apiMock.thread.mockResolvedValue(thread);
    apiMock.retrySummary.mockResolvedValue({ status: "generating" });
    render(<App />);

    expect(await screen.findByText("服务端生成的摘要")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /摘要主题/ }));
    expect(await screen.findByRole("button", { name: "重试摘要" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试摘要" }));
    await waitFor(() => expect(apiMock.retrySummary).toHaveBeenCalledWith("email-42"));
  });
});

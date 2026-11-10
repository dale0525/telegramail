# Telegram Bot API: HTML 渲染能力的边界与富消息

本文回答一个具体问题：Telegram 机器人能给用户发送什么样的 HTML。结论以官方 Bot API 文档与生产环境实测为准。

## 两种能力，不要混为一谈

`parse_mode=HTML` 与富消息（Rich Message）是两套互不兼容的渲染管线，混用会直接失败。

1. `parse_mode=HTML` 只解析**行内实体**：`b`、`i`、`u`、`s`、`tg-spoiler`、`code`、`pre`、`a`、块引用等。它不接受文档结构级标签，把 `<h3>` 或 `<table>` 塞进 `sendMessage` 会返回 `400 Bad Request: Unsupported start tag "h3"`。这条限制是渲染器的，不是参数配错。
2. 富消息走 `sendRichMessage`，正文放在 `rich_message.html` 里，支持标题、段落、列表、任务清单、表格、折叠块（`details`）、引用、媒体块、公式，以及内嵌按钮行（`<tg-button-row>`）。

## 富消息的实际约束

以下为实测确认的行为，未在文档中明确承诺的部分以实测为准。

1. 富消息**不接受 `parse_mode`**。正文只能是富消息自己的 HTML 方言，行内实体按它的规则解释。
2. 富消息**可以携带 `reply_markup`**，因此内联键盘照常使用。
3. 富消息**可以用 `editMessageText` 携带 `rich_message` 编辑**，摘要刷新这类流程可以复用同一条消息。
4. **不受 4096 字符文本上限约束**，长内容不必预先切片。
5. 只改键盘的操作（`editMessageReplyMarkup`）与 `deleteMessage` 对富消息同样有效，因此「删除确认」这类只换按钮的交互无需感知消息形态。

## 编辑既有消息时的形态一致性

一条消息一旦以某种形态发出，后续编辑必须用同一形态，否则请求失败。

| 原始消息形态 | 正确的编辑方式 |
| --- | --- |
| 文本（`sendMessage`） | `editMessageText` + `text` |
| 文档（`sendDocument`，图片/HTML 等） | `editMessageCaption` + `caption` |
| 富消息（`sendRichMessage`） | `editMessageText` + `rich_message` |

调用方必须持久化「这条消息当初是什么形态」，不能靠推断。只改键盘或用 `deleteMessage` 时无需区分形态。

## 能力探测字段的语义

`getMe` 返回的话题相关布尔字段用途不同，不能互相替代。

1. `has_topics_enabled`：机器人是否在私聊中启用了话题模式。
2. `allows_users_to_create_topics`：机器人是否允许**用户**在私聊里创建和删除话题。它描述的是用户权限，**不是**机器人自身能否创建话题。

因此 `allows_users_to_create_topics=false` 与「机器人建话题失败」并不矛盾，不能当作机器人建话题能力的判据。判据只能来自实际调用 `createForumTopic` 的结果。

## 一条容易被忽略的投递一致性要求

把主消息从文档改为富消息后，原本作为主消息的文档会变成静默附件。若投递层按「主消息 + 额外分片」记账，新产生的附件必须同样计入分片记录：无话题的降级投递是靠遍历分片表逐条 `deleteMessage` 来删除整条线程的，漏记的消息 id 会在删除后残留成孤儿。

## 参考

- Bot API 官方文档（`sendRichMessage`、`rich_message`、`getMe` 字段说明）：https://core.telegram.org/bots/api
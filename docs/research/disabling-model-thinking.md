# 关闭上游模型思考的请求写法

本文记录在 OpenAI 兼容网关上关闭模型思考时，实际有效与无效的请求字段。结论来自 2026-11-09 对 `https://hub.logiconsole.com/v1` 的实测。

## 结论

只有 OpenAI 标准的 `reasoning_effort` 字段能关闭思考，取值 `"none"`。

其余常见写法在该网关上一律无效：思考照常发生，token 照常消耗。

## 实测数据

同一问题（`What is 2+2? Answer with just the number.`），模型 `group/deepseek-flash`，比较 `usage` 中的思考开销：

| 请求额外字段 | 思考 token | 结论 |
| --- | --- | --- |
| 无（基线） | 19 | 思考默认开启 |
| `reasoning_effort: "none"` | 0 | 有效 |
| `thinking: {"type": "disabled"}` | 13 | 无效 |
| `enable_thinking: false` | 19 | 无效 |
| `chat_template_kwargs: {"enable_thinking": false}` | 14 | 无效 |
| `reasoning: {"enabled": false}` | 19 | 无效 |

`reasoning_effort: "none"` 生效时，除思考 token 归零外 `prompt_tokens` 也从 43 降到 17，说明部分网关在关闭思考后会省掉额外的模板开销。

## 对该网关的判据

判断“思考是否已关闭”看 `usage.completion_tokens_details.reasoning_tokens` 是否为 0，不要看响应正文长度或往返耗时——思考内容不进 `content`，无法从输出直接判断。

## 代码中的落点

`app/llm/openai.py` 在 `disable_thinking` 开启时，对 `stream_completion` 与 `generate_completion` 两条路径都附加 `reasoning_effort: "none"`。该字段默认不发送，避免把非标准字段推给不认识它的第三方端点。
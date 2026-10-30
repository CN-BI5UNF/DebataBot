# 图像识别（Vision）

让 Karen 能看懂图片——用户发了图，AI 看了再回复。

## 先分清两条路径

| 方式 | 设置位置 | 适用场景 |
|---|---|---|
| **主模型原生看图** | 设置 → 功能 → 主模型原生图片输入 | 聊天模型本身支持图片输入 |
| **独立视觉 `describe_image`** | 设置 → 功能 → 独立视觉识别 | 聊天模型不能看图，或想把看图交给另一个模型 |

两者互不影响，也不会自动双调用：原生开关打开时，图片直接进主模型请求；
独立视觉只在主模型按需调用 `describe_image` 工具时使用。

## 情况 A：主模型能看图（推荐）

1. 确认聊天模型是带视觉能力的预设模型（预设能力表里标记了 `vision`）。
2. 设置 → 功能 → **主模型原生图片输入** 选「自动」或「开启」。
3. **独立视觉识别保持关闭**，不需要填视觉 provider、base_url 或 API Key。

说明：

- `自动`：只对预设中标记了 `vision` 的聊天模型附图。
- `开启`：显式声明模型可接收图片；仍需协议支持图片转换，否则不会附图。
- `关闭`：不向主模型附图，独立视觉仍可按需使用。

主模型是自定义模型、没有预设能力信息时，`自动` 无法判断；确认模型支持图片
后再选「开启」。

## 情况 B：主模型不能看图

1. 先到 模型管理 →「添加提供商」，添加一个支持视觉的 provider（如智谱、
   火山方舟、OpenAI 等）。
2. 设置 → 功能 → 打开「启用独立视觉识别（describe_image）」。
3. **视觉 provider** 选择刚才添加的 provider。这个字段引用已有 provider，
   不是新建 provider。
4. **视觉模型** 填独立的看图模型 ID，例如 `glm-4v`、`qwen-vl-max`。
   它可以和聊天模型不同。
5. 选择已有 provider 时，**视觉独立 base_url / API Key 留空即可**，会复用该
   provider 的地址和密钥。
6. 只有视觉端点没有登记成 provider 时，才选择「自定义 OpenAI 兼容端点」，
   填写 base_url 和 API Key。
7. 点「保存视觉设置」，然后重启 Runtime。

## 字段说明

- **视觉 provider**：引用「模型管理」里已添加的 provider ID。选择预设
  provider 后，地址、协议和密钥默认都复用它。
- **视觉独立 base_url**：可选覆盖；自定义视觉端点必填。
- **视觉独立 API Key**：写入加密的 `secrets.enc`；选择已有 provider 时可留空
  复用其密钥。
- **每图 token 预留 / 单请求最大图片数**：项目资源护栏，不是供应商精确账单。
- 关闭独立视觉会保留 provider、模型和密钥，不会删除配置。

## 获取 API Key

- [智谱](https://open.bigmodel.cn/usercenter/apikeys)
- [阿里百炼](https://bailian.console.aliyun.com/)
- [火山方舟](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey)
- [OpenAI](https://platform.openai.com/api-keys)
- [Anthropic](https://console.anthropic.com/settings/keys)
- [Gemini](https://aistudio.google.com/apikey)

## 常见问题

**主模型能看图，还需要独立视觉吗？** 不需要。只开「主模型原生图片输入」即可；
独立视觉是可选工具。

**视觉 provider 和模型页的 provider 是什么关系？** 是同一个 provider 的引用。
你在模型管理添加一次，视觉设置里直接选它，不要再填一遍地址和 Key。

**保存后没生效？** Vision 配置需要重启 Runtime；设置页会提示「立即重启」。

**图片太大？** 压到 5MB 或 4K 以下再发。

> [English](17-REAL_ASSETS_INDEX.en.md)

# 17 · 真实素材索引

这页现在只保留一件事：

> 记录当前公开页面真正还在用哪些素材，以及后续复拍时该守什么规则。

先说定位：

- 这页是维护者附录
- 不是普通用户默认入口
- 普通用户优先看 `README`、`15`、`18`

---

## 1. 当前公开页仍在用的主素材

如果某个素材不在这份清单里：

- 默认按维护者侧边产物理解
- 不要继续把它挂到用户入口页
- 不要把它当成公开稳定资产

### 页面

- `23-PROFILE_CAPABILITY_BOUNDARIES.html`
- `23-PROFILE_CAPABILITY_BOUNDARIES.en.html`

### 视频

- onboarding 文档引导视频（中英）
- capability tour 视频（中英）
- ACL 场景视频（中英）

### 关键截图

- onboarding 未安装 / 已安装截图
- Dashboard visual memory 截图
- ACL 截图
- Memory Palace skills/chat 证据截图
- `23` 当前仍直接嵌入的那一张 profile boundary chat 截图
- `16`、`GETTING_STARTED`、`TECHNICAL_OVERVIEW` 仍在直链的 dashboard 页面截图

### Mermaid fallback 图

Mermaid 静态 fallback PNG 也属于公开资产，因为它们现在被公开 Markdown 直接嵌入。

维护规则只有一条：

- Mermaid 文案、节点或顺序变了，就把 `.mmd`、生成的 `.png`、以及 Markdown 引用一起更新

---

## 2. 哪些内容已经不再属于默认公开面

下面这些内容，不要再写成默认公开给用户看的稳定素材：

- 没有被公开页面直接嵌入的旧 `profile-matrix/*` 库存截图
- 除公开文档仍在直链的页面图之外，其余 `dashboard-current/*` 库存截图
- 只剩维护者复核价值、但已经不在公开叙事里的重复截图

只要它们已经不再被公开页面直接引用，就按下面这条边界处理：

- 不把它们继续当成公开稳定资产
- 不在公开文档里继续承诺它们存在
- 不再需要时，就从公开仓库里移走

---

## 3. 公开素材安全规则

在复用或重拍截图 / 视频之前，先检查：

- 用户名
- 本机绝对路径
- 私有 key 或私有 endpoint
- 不应该被用户读成公开承诺的宿主内部状态

拿不准时就按这条规则：

- 不要放进公开入口页
- 先当成维护者侧边材料处理

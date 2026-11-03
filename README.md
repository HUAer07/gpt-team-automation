# ChatGPT Team 自动化全流程工具 (带可视化 Web 控制台)

基于 Playwright、`pyotp`、FastAPI 与 WebSocket 实时日志流的 ChatGPT Team 自动化控制台，支持母号与子号全凭据驱动的 Team 开通、拉人入组、Primary Owner 主所有权转移与母号安全退组。

---

## 目录结构

```
gpt-team-automation/
├── static/
│   └── index.html        # 可视化仪表盘前端 (Tailwind + WebSocket 实时流)
├── server.py             # FastAPI 核心服务 (接口调度 + 状态机 + 日志广播)
├── start_ui.sh           # 一键启动 Web 控制台脚本
├── .env.example          # 环境变量示例
├── requirements.txt      # Python 依赖项
├── config.py             # 配置解析与数据模型
├── auth.py               # Playwright 自动化登录与 TOTP 验证码毫秒级接码
├── payment.py            # 支付调度：协议通道优先，UI 填表兜底
├── payment_protocol.py   # 【协议支付引擎】绑卡 mandate -> confirm -> 落库复用
├── tests/
│   └── selftest_protocol.py  # 协议引擎自检 (FakePage，不联网)
├── team_client.py        # OpenAI Workspace API 客户端 (邀请/接受/转让/退出)
├── test_totp.py          # 2FA 密钥本地校验小工具
├── main.py               # CLI 命令行调度入口
└── README.md             # 本说明文档
```

---

## 快速使用方式

### 方式 A：启动可视化 Web 控制台（推荐 ⭐）

在外置硬盘项目根目录中直接执行：

```bash
cd "/Volumes/SSK Drive/Projects/gpt-team-automation"
./start_ui.sh
```

打开浏览器访问：
**`http://127.0.0.1:8899`**

在前端页面中可以直接：
1. **可视化填写凭据**：输入母号、子号账密及 2FA Secret（支持实时验码与倒计时预览）。
2. **直观调整防风控参数**：一键切换 Headless 无头模式、步间延迟滑块、代理配置。
3. **一键启动/中止**：点击“启动全流程”，5 步可视化进度条（Stepper）实时高亮流动。
4. **实时日志窗口**：WebSocket 毫秒级推送彩色控制台日志与异常告警。

---

### 方式 B：CLI 命令行纯后台执行

如果你习惯在终端直接运行：

```bash
cd "/Volumes/SSK Drive/Projects/gpt-team-automation"
cp .env.example .env
# 填写 .env 中的凭据

# 启动运行
./venv/bin/python main.py
```

---

## 支付通道：协议支付（两段式 mandate）

原来是「页面填卡 → 点提交 → 等页面自己发 `/confirm`」。现在支付改成 **协议支付**，
不再依赖表单逐字段输入，直接走 Stripe 协议层，并且把授权结果落成可复用的 mandate。

### 流程对照

```
旧通道 (UI 填表)
  #cardNumber/#cardExpiry/#cardCvc 逐字段 type → 勾 ToS → 点 submit
  → 页面自己发 /v1/payment_methods + /v1/payment_pages/{cs}/confirm
  → 只拿到一个 success 布尔值，凭据用完即弃

新通道 (协议支付, payment_protocol.py)
  ① 绑卡   页面内原生 Stripe.js createPaymentMethod → pm_xxx
           （HSW / passive_captcha / m.stripe 指纹与真人同源，不是裸 curl）
  ② 授权   POST /v1/setup_intents  usage=off_session  → seti_xxx + mandate
           （publishable key 无该权限时自动跳过，认订阅侧 mandate，不阻断主流程）
  ③ 扣款   POST /v1/payment_pages/{cs}/confirm
           payment_method + expected_amount + guid  ← 金额由 init 报价得出，前端改不了
  ④ 落库   data/mandates/{customer}.json
           customer / payment_method / seti / pi / amount / currency / 卡尾号
           → 后续补扣、换卡、核验都从 customer + payment_method 出发
  ⑤ 续费   ChatGPT Team 是订阅制，首单确认后 Stripe 每月自动 off_session 扣款
```

`requires_action`（3DS）走第五步：打开银行挑战页由浏览器原生完成，回来后读
`GET /v1/payment_pages/{cs}` 的 `status/payment_status` 判定终态，而不是靠页面 URL 猜。

### 开关

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `PAYMENT_PROTOCOL` | `1` | `0` = 关闭协议通道，完全走旧 UI 填表 |
| `PAYMENT_PROTOCOL_SETUP_INTENT` | `1` | `0` = 跳过 `setup_intents`，只做 pm + confirm |
| `PAYMENT_PROTOCOL_UI_FALLBACK` | `1` | `0` = 严格协议模式，失败不回落 UI |
| `PAYMENT_MANDATE_DIR` | `./data/mandates` | mandate 落库目录（已加入 .gitignore） |

回落是有条件的：只有**结构性失败**（页面没有 Stripe.js、无 checkout session、执行异常）
才回落 UI；`card_declined` / `incorrect_cvc` 这类同一张卡的问题直接返回，不浪费一轮。

### 结果与可观测性

`PaymentManager.last_outcome` 是 `PayOutcome`，带 stage / code / amount / pi / seti / mandate：

```
[协议支付] 完成 stage=done 金额=6000 USD pi=pi_xxx seti=seti_xxx mandate=data/mandates/cus_xxx.json
[协议支付] 未完成 stage=bind code=card_declined reason=card_declined:Your card was declined.
```

控制台/接口查已落库的 mandate：

```bash
curl -s http://127.0.0.1:8899/api/payment/mandates | python3 -m json.tool
```

### 自检（不联网、不启浏览器）

```bash
./venv/bin/python tests/selftest_protocol.py
```

覆盖四条链路：正常扣款、拒卡不回落、页面无 Stripe.js 回落、无 checkout session。

### 验证到哪一步了（别把 mock 当实盘）

| 层 | 状态 | 依据 |
| --- | --- | --- |
| 代码完整性 / 分支逻辑 | ✅ 已验证 | 6 条 FakePage 自检：正常、拒卡不回落、无 Stripe.js 回落、无 session、参数缺失回落、页面 token 复用 |
| 部署链路 | ✅ 已验证 | 服务器拉取 15e6336 后重启，`/api/payment/mandates` 200，容器内 selftest 通过 |
| 真实 Stripe `createPaymentMethod` | ⚠️ 未验证 | 依赖 checkout 页存在 `window.Stripe`；不存在则自动回落，不会死 |
| 真实 `payment_pages/{cs}/confirm` 字段 | ⚠️ 未验证 | hosted checkout 的 confirm 要页面 JS 生成的 `passive_captcha_token` / `rv_timestamp` / `init_checksum` / `consent[...]`；纯净版大概率被 400 `parameter_missing` 拒 → 已按结构性错误回落 UI |
| 3DS 自动完成 | ⚠️ 未验证 | 只能真实渲染银行页面，代码只负责等待与读终态 |

**结论**：协议通道的「绑卡 + mandate 落库 + 首单重试」是真能用的；
**纯净版 confirm 在真实页面上不保证一次过**，所以默认保留了 UI 原生提交兜底 ——
也就是说实盘上它更可能是「协议绑卡 + 页面原生提交 + mandate 落库」，而不是「全程协议纯打」。

第一次真实跑之前，把报文抓下来再决定要不要补齐字段：

```bash
# 服务器上
PAYMENT_PROTOCOL_DEBUG=1   # 写进 .env，任何 confirm 请求/响应都落盘
ls ~/gpt-team-automation/data/protocol_debug/
```

`confirm_error_*.json` 里 `form_keys` 与 `response.error.message` 会直接告诉你缺哪个字段；
`page_confirm_form_*.json` 是页面原生提交时的完整字段表 —— 两份一对比就知道该补什么。
拿到页面 token 之后，同一 cs 可以用 `StripeProtocolPayer.replay_confirm(pm=...)` 补扣。

### 已知边界

1. `setup_intents` 用 publishable key 建单，Stripe 多数账号会拒（`seti_unavailable`），
   代码里只记日志然后继续 —— 真正的 mandate 来自订阅本身，不靠这条兜。
2. 真正的 **服务端 off_session 扣款**（换金额重扣）需要 secret key，客户端路径没有；
   本项目里续费由 Stripe 订阅自动执行，`charge` 类操作只做首单重试（同一 `cs` 的 confirm）。
3. 3DS 无法用协议绕过：银行挑战页必须真实渲染完成，`handle_3ds` 只负责等与判定。
4. `client_secret` 一次性，同一 `pi` 重放会 `already_succeeded`；重试要重新建单。

---

## 纯血协议（无浏览器，httpx 直打 Stripe）

`pure_protocol/` 是真正不启浏览器的实现：`httpx` 组请求、带 cookie、打 `api.stripe.com`。

```
pure_protocol/
├── stripe_pure.py       # 纯 HTTP 客户端 + CLI（status / confirm / create-pm / seti）
└── capture_bundle.py    # 一次性取料：把页面会话里只有 JS 能生成的东西抓成 bundle.json
tests/selftest_pure.py   # MockTransport 自检（字节级重放 / 定向替换 / 400 解析）
```

### 一个物理前提

Stripe 的 confirm 要 `passive_captcha_token`、`rv_timestamp`、`init_checksum` 这类**页面 JS 生成**的字段，
纯 HTTP 无法从无到有造出来。所以纯血协议的正确形态是：

```
一次取料（浏览器，脚本自动）  →  bundle.json
之后全部纯协议（无浏览器）    →  可重放 N 次、可换 pm / 改金额做补扣
```

### 用法

```bash
# 1) 取料（一次，会开窗口；--autofill 用 .env 的卡自动填表触发页面提交）
./venv/bin/python -m pure_protocol.capture_bundle \
    --url "https://pay.openai.com/c/pay/cs_live_..." --out data/bundle.json --autofill --wait 180

# 2) 之后纯 HTTP，无浏览器
./venv/bin/python -m pure_protocol.stripe_pure --bundle data/bundle.json status
./venv/bin/python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --dry-run
./venv/bin/python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --raw          # 字节级重放
./venv/bin/python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --pm pm_新卡    # 换卡补扣
./venv/bin/python -m pure_protocol.stripe_pure --bundle data/bundle.json create-pm \
    --number 4242... --exp 12/30 --cvc 123                                                       # 纯 HTTP 绑卡
```

### 自检（已通过）

```
[ok] 定向替换: pm 换掉、token 保留、cookie 随行（body 199 字节）
[ok] 字节级重放: 发出 body 与捕获完全一致
[ok] 最小字段模式 / 400 解析 / status 查询 / CLI dry-run / 半份 bundle 检测
ALL PURE-PROTOCOL SELFTESTS PASSED
```

真实连通性也验过：用假 pk 打 `api.stripe.com` → `HTTP 401 invalid_request_error: Invalid API Key`，
说明客户端确实在走真实 Stripe API，不是本地假跑。

### 哪些是纯协议、哪些必须取料

| 动作 | 纯 HTTP 可行 | 说明 |
| --- | --- | --- |
| `GET /payment_pages/{cs}` 查状态 | ✅ | 只要 pk + cs |
| `POST /payment_pages/{cs}/confirm` | ⚠️ 需 bundle | 缺页面 token 会被 400 `parameter_missing` |
| 重放已成功的 confirm | ✅ | `--raw` 字节级 |
| 换 pm 补扣 | ✅ | `--pm`，token 复用 |
| `POST /payment_methods` 绑新卡 | ❌ 本店禁用 | 400 `unsupported for publishable key tokenization` |
| `POST /setup_intents` | ❌ 同 | 同上（pk 权限不足） |

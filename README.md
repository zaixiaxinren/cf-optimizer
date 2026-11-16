# CF 优选 IP · 自动测速与推送

[![Build and push image](https://github.com/YOUR_GITHUB_USER/cf-optimizer/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/YOUR_GITHUB_USER/cf-optimizer/actions/workflows/docker-publish.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/YOUR_DOCKERHUB_USER/cf-optimizer)](https://hub.docker.com/r/YOUR_DOCKERHUB_USER/cf-optimizer)
[![Image Size](https://img.shields.io/docker/image-size/YOUR_DOCKERHUB_USER/cf-optimizer/latest)](https://hub.docker.com/r/YOUR_DOCKERHUB_USER/cf-optimizer)
[![Platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-blue)](https://hub.docker.com/r/YOUR_DOCKERHUB_USER/cf-optimizer)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[CloudflareST](https://github.com/XIU2/CloudflareSpeedTest) 的容器化封装：**定时**从多个公开 IP 列表拉取候选 → 延迟测速 → **并行**下载测速 → 按阈值筛选 → 自动推送到 [CFNext](https://github.com/PAICNI/CFNext) 面板的「优选 IP」列表。

自带 WebUI（`http://<你的NAS>:8090`），也可以只用 docker compose 跑，不依赖任何第三方 Python 库。

```bash
docker run -d --name cf-optimizer --restart unless-stopped \
  -p 8090:8090 -v $PWD/data:/data -v $PWD/out:/out \
  YOUR_DOCKERHUB_USER/cf-optimizer:latest
```

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ 候选列表 URL │ → │ 解析成纯 IP  │ → │ 延迟 + 下载  │ → │ CFNext 面板  │
│ 可填多个/粘贴 │   │ 域名→IP 去重 │   │ 测速（并发） │   │ 优选 IP 列表 │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

---

## 特性

| 能力 | 说明 |
|---|---|
| **多个候选来源** | 可填任意多个公开 txt 地址，也可以直接粘贴；合并后统一去重，同一个 IP 只测一次 |
| **输入格式自适应** | `1.1.1.1`、`1.1.1.0/24`、`1.1.1.1:8443`、`1.1.1.1:443#备注`、**域名**（自动 DNS 解析成 IP）都能直接吃 |
| **并行下载测速** | cfst 自带的下载测速是串行的，本项目自研线程池同时测多个 IP，并发数可调 |
| **进度条** | WebUI 实时显示「已测 / 总数 / 还剩多少个 / 可用数」，分延迟、下载两个阶段 |
| **定时任务在网页上配** | 开关、频率、cron 表达式、是否自动推送，全部网页操作，保存即生效 |
| **后缀可自定义** | 用源列表自带的 `#备注` / 数据中心码（`#LAX`）/ 固定文本 / 自定义模板 |
| **一键推送** | 直写 CFNext 面板的 `preferredIPs`，写前读旧值、写后回读校验 |
| **零第三方依赖** | 后端是 Python3 标准库，不用 pip、不用 Node、不用数据库 |

---

## 快速开始

### 方式 A：直接用现成镜像（推荐）

不用 clone 代码、不用构建，一条命令拉起来。

```bash
mkdir -p cf-optimizer && cd cf-optimizer
mkdir -p data out

cat > docker-compose.yml <<'EOF'
services:
  cf-optimizer:
    image: YOUR_DOCKERHUB_USER/cf-optimizer:latest
    container_name: cf-optimizer
    restart: unless-stopped
    ports:
      - "8090:8090"
    volumes:
      - ./data:/data
      - ./out:/out
    environment:
      TZ: Asia/Shanghai
EOF

docker compose up -d
docker compose logs -f     # 看到 [init] Web 控制台已启动 即可
```

镜像支持 **amd64 / arm64** 双架构，群晖、威联通、飞牛、树莓派都会自动拉对架构。

> 只想要 Docker 命令、不用 compose 也可以：
> ```bash
> docker run -d --name cf-optimizer --restart unless-stopped \
>   -p 8090:8090 -v $PWD/data:/data -v $PWD/out:/out \
>   -e TZ=Asia/Shanghai \
>   YOUR_DOCKERHUB_USER/cf-optimizer:latest
> ```

### 方式 B：自己构建

适合想改代码、或在内网无法拉镜像的场景。

```bash
git clone https://github.com/YOUR_GITHUB_USER/cf-optimizer.git
cd cf-optimizer

# 把 compose 里的 image 换成 cf-optimizer:local，并解开 build 那行
sed -i 's#^    image:.*#    image: cf-optimizer:local#; s#^    \# build: \.#    build: .#' docker-compose.yml

docker compose up -d --build
```

首次构建会从 GitHub 下载 CloudflareST（约 8 MB）。国内网络慢可以把 `cfst` 二进制直接放到构建目录，
Dockerfile 会优先用本地文件，离线也能构建；也可以用代理：

```bash
docker compose build --build-arg HTTPS_PROXY=http://<宿主机IP>:7890
```

固定 CloudflareST 版本（可复现构建）：

```bash
docker compose build --build-arg CFST_VERSION=2.3.5
```

### 打开网页

浏览器访问 `http://<宿主机IP>:8090`：

1. 首次打开会弹引导，要求填 **CFNext 面板入口地址**（含入口路径，例如 `https://xxx.pages.dev/vvbo`）和管理密码
2. 填好后左栏填候选列表地址，点 **开始测速**
3. 出结果后点 **推送到 CFNext**

### 镜像标签

| 标签 | 说明 |
|---|---|
| `latest` | 最新稳定版 |
| `1.2.3` | 固定版本，生产建议用它，避免 `latest` 变更带来意外 |
| `1.2` | 同一小版本的最新补丁 |

---

## 目录结构

```
.
├─ docker-compose.yml     # 编排（端口 8090 + 两个挂载）
├─ Dockerfile             # alpine + bash/curl/jq/python3 + cfst（多架构）
├─ entrypoint.sh          # 容器入口：写 crontab、起 Web 服务、看守 crond
├─ run.sh                 # 定时任务的一轮完整流程（命令行入口）
├─ server.py              # Web 后端（Python3 标准库）
├─ static/index.html      # WebUI 单页（无框架、无 CDN）
├─ resolve.py             # 候选解析：域名→IP、拆分 IP:端口#备注、剔除非 CF 段
├─ dltest.py              # 并行下载测速（线程池）
├─ format_result.py       # cfst CSV + 元数据 → IP:端口#后缀
├─ push_panel.sh          # 登录 CFNext → 写 preferredIPs → 回读校验
├─ publish.sh             # 本地构建并推送到 Docker Hub
├─ .github/workflows/     # 打 tag 自动多架构构建推送
└─ worker/                # 可选：方案 B 的 Cloudflare Worker（PUSH_MODE=webhook 时才用）
```

运行时产物：

```
/data/web-config.json          # 网页上保存的全部配置（权限 600，含面板密码）
/data/ip.txt                   # 最近一次拉取的候选列表
/out/result.txt                # 最近一轮结果（每行 IP:端口#后缀）
/out/result-YYYYMMDD-HHMM.txt  # 历史结果，默认留 20 份
```

---

## 网页参数说明

左栏从上到下：

### 候选来源

- **候选列表网络地址**：可加任意多条，逐个拉取后合并去重；某个地址挂了只记一行日志，不影响其它
- **直接粘贴**：和上面的地址一起用，适合临时贴几个 IP

每行的格式（都支持）：

```
1.1.1.1                       # 纯 IP
1.1.1.0/24                    # 网段（cfst 默认每 /24 随机取一个 IP）
1.1.1.1:8443                  # 指定端口
1.1.1.1:443#香港优化           # 端口 + 备注（备注会原样作为后缀）
www.shopify.com:443#企业域名   # 域名（自动 DNS 解析成 IP）
# 这是注释                      # # 开头整行忽略
```

> 域名解析后会按内置的 Cloudflare 官方网段过滤，非 CF 的 IP（例如 Shopify、其他 CDN）会被剔除——
> 否则测出来再快也不能当优选 IP 用。

### 筛选参数

| 参数 | 默认 | 说明 |
|---|---|---|
| 端口 | 443 | 测速与输出使用的端口 |
| 延迟上限 | 350 ms | 超过就丢弃。**先跑一轮看实际延迟再定**，设太低会 0 条结果 |
| 速度下限 | 0 MB/s | 0 = 不按速度过滤。想筛就设 1~2，别超过实际带宽 |
| 保留条数 | 10 | 最终最多留下几个 |
| 并发线程 | 100 | cfst 延迟测速线程数，弱设备别开太高 |
| Ping 次数 | 4 | 单个 IP 延迟测速次数 |
| 测速地址 | `speed.cloudflare.com/__down?bytes=50000000` | 下载测速用的 URL |

### 并行下载测速

| 参数 | 默认 | 说明 |
|---|---|---|
| 下载测速数量 | 10 | 从延迟最低的 IP 里取几个测下载速度 |
| **并行下载数** | 4 | **同时测几个 IP**（1~32） |
| 单个测速时长 | 10 s | 单个 IP 最多测多久 |
| 跳过下载测速 | 关 | 勾上只比延迟，快很多，但没有速度数据 |

**关于并发的取舍**：并发越高越省时间——4 个 IP、单个 6 秒，串行要 24 秒，并发 4 只要 6 秒。
但多个 IP 抢同一条出口带宽，测出来的速度会失真（可能偏高也可能偏低，取决于拥塞和服务器分配）。
**想要可横向比较的准确数值，把并行数设成 1**；只想快速筛掉明显不通的，设 4~8 都行。

> 顺带修掉一个坑：cfst 在「只测延迟」（`-dd`）模式下，结果里的「地区码」列恒为 `N/A`。
> 本项目在下载测速时读取响应的 `cf-ray` 头把数据中心补上，所以「数据中心」后缀仍能正常显示。

### 输出后缀

| 模式 | 输出示例 | 说明 |
|---|---|---|
| 源列表里的 `#备注` | `104.16.0.1:443#教育域名 \| 岭南大学` | 推荐。没有备注时回退到数据中心码 |
| 数据中心 | `104.16.0.1:443#LAX` | 用 cfst 识别出的地区码 |
| 固定文本 | `104.16.0.1:443#香港优化` | 全部用同一个后缀 |
| 不带后缀 | `104.16.0.1:443` | 干净格式 |
| 自定义模板 | `104.16.0.1:443#LAX-194ms` | 支持 `{colo}` `{latency}` `{speed}` |

### CFNext 面板

| 参数 | 说明 |
|---|---|
| 面板入口地址 | 登录时浏览器地址栏里那个完整地址，**含入口路径**（如 `https://xxx.pages.dev/vvbo`） |
| 面板管理密码 | 存在 `/data/web-config.json`；**接口不会把密码回传给浏览器**，页面上只显示「已保存」。想改就重输，留空表示不修改 |

### 定时任务

| 项 | 说明 |
|---|---|
| 启用定时自动测速 | 关掉就只在你点「开始测速」时跑 |
| 运行频率 | 每 1/2/4/6/12 小时、每天定点、每周一，或「自定义…」 |
| cron 表达式 | 标准五段式（分 时 日 月 周），下方实时显示**下次运行时间** |
| 定时跑完自动推送到 CFNext | 关掉就只写本地 `/out` |

保存后**立即生效**：服务端重写容器内的 `/etc/crontabs/root`，并让 crond 重新加载，不需要重启容器。
顶部还有一个「下次 明天 08:00」的小标记。

写法示例：

```
0 */2 * * *      每 2 小时
0 8,20 * * *     每天 8:00 和 20:00
30 3 * * 1       每周一 3:30
*/15 * * * *     每 15 分钟
```

---

## 参数优先级

```
网页上保存的配置  >  docker-compose 里的环境变量  >  脚本内置默认值
```

所以**日常只改网页就够了**，compose 里的值只是首次使用的兜底。
定时任务的命令行（`run.sh`）每轮都会重新读一次网页配置，不会出现「网页改了、定时还在用旧值」。

环境变量完整列表：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TZ` | — | 时区，建议 `Asia/Shanghai` |
| `WEB_PORT` | `8090` | WebUI 端口 |
| `CRON` | `0 */2 * * *` | 仅作初始兜底，网页设置过之后不再读取 |
| `RUN_ON_START` | `0` | 容器启动时是否立刻跑一轮 |
| `LATENCY_MAX` | `350` | 延迟上限（ms） |
| `SPEED_MIN` | `0` | 速度下限（MB/s） |
| `TOP_N` | `10` | 保留条数 |
| `PORT` | `443` | 测速端口 |
| `THREADS` | `100` | 延迟测速线程数 |
| `PING_TIMES` | `4` | 延迟测速次数 |
| `SPEED_URL` | `speed.cloudflare.com/...` | 下载测速地址 |
| `DISABLE_DL` | `0` | `1` = 跳过下载测速 |
| `DL_COUNT` | `10` | 下载测速数量 |
| `DL_PARALLEL` | `4` | 并行下载测速数量 |
| `DL_TIME` | `10` | 单个 IP 测速时长上限（秒） |
| `SUFFIX_MODE` | `source` | `source` / `colo` / `fixed` / `none` |
| `TAG` | `auto` | `SUFFIX_MODE=fixed` 时使用；数据中心识别失败也回退到它 |
| `KEEP_HISTORY` | `20` | 历史结果保留份数 |
| `PUSH_MODE` | `panel` | `panel` 直写面板 / `webhook` POST 到自建 Worker / `none` 只留本地 |
| `PANEL_URL` | — | 面板入口（网页没填时兜底） |
| `PANEL_PASSWORD` | — | 面板密码（网页没填时兜底，建议放 `.env`） |
| `PANEL_FORCE_CUSTOM` | `1` | 推送时把订阅模式切成「只用优选 IP」 |
| `CF_ONLY` | `1` | 只保留 Cloudflare 官方段的 IP |
| `WEBHOOK_URL` / `WEBHOOK_TOKEN` | — | `PUSH_MODE=webhook` 时使用 |

---

## 工作流程（一轮任务做了什么）

1. **拉取候选**：依次下载 `source_urls` 里的每个 txt，加上手填内容，合并
2. **解析**：域名走 DNS 解析；`IP:端口#备注` 拆开（端口和备注存进映射表）；按 CF 官方网段剔除外部 IP；去重
3. **延迟测速**：`cfst -dd`（只测延迟），产出按延迟升序的 CSV
4. **并行下载测速**：取延迟最低的 `DL_COUNT` 个，用 `DL_PARALLEL` 个线程同时下载 `DL_TIME` 秒，
   算出 MB/s，顺便从 `cf-ray` 头取数据中心
5. **筛选**：按速度下限过滤、按速度降序排列，取前 `TOP_N` 个，拼成 `IP:端口#后缀`
6. **推送**（定时任务且勾了自动推送时）：写进面板的 `preferredIPs`，回读校验

输出文件 `/out/result.txt`：

```
104.18.34.119:443#教育域名 | 岭南大学
162.159.140.198:443#企业域名 | 山姆会员
172.64.146.59:443#LHR
```

---

## CFNext 接口说明（v3.0.0 实测）

脚本不是猜的，是照着页面源码 + 实测校准出来的。**有两个坑，踩了会一直拿不到数据**：

**坑 1：`/vvbo` 只是登录入口，API 不在它下面。**

登录后内部 API 前缀是另一条 uuid 路径，写在控制台页面的 `var APIPATH = "..."` 里。
脚本每次运行都会先登录、抓控制台页、提取这个值——所以面板改了路径也不用改脚本。

**坑 2：推送是「整体替换」，不是追加。**

`POST <APIPATH>/api/config` 传 `preferredIPs` 会把面板上的列表**整份换掉**。
`run.sh` 在没有结果时会直接退出、不清空面板，但如果你手动推了一轮不合意的结果，原来的列表就没了。

请求流程：

```bash
# 1) 登录（form 表单），拿 cookie
curl -c ck.txt -X POST -d "password=<密码>" https://<面板>/vvbo
# → {"ok":true,"next":"/vvbo"}

# 2) 抓控制台页，提取 APIPATH
# → var APIPATH = "/ccd9fb75-...."

# 3) 读配置（注意响应外面包了一层 data）
curl -b ck.txt https://<面板>/<APIPATH>/api/config
# → {"ok":true,"data":{"preferredIPs":[...],"optimizer":{...}}}

# 4) 写回
curl -b ck.txt -X POST https://<面板>/<APIPATH>/api/config \
  -H 'Content-Type: application/json' \
  -d '{"preferredIPs":[{"ip":"104.18.34.119","port":443,"name":"LHR"}]}'
```

---

## 常见问题

**Q：跑完 0 条结果怎么办？**

按这个顺序查：

1. 看日志里「解析候选」那行——是不是解析出 0 个 IP（地址填错 / 列表里都是非 CF 的 IP）
2. 延迟上限是不是设得太严。先看 cfst 输出的实际平均延迟，再往上留 100ms 余量
3. 速度下限是不是超过实际带宽。家用宽带跑到 10 MB/s 以上就很难，建议先设 0
4. 勾「跳过下载测速」跑一次，如果这时有结果，说明问题出在下载测速环节

**Q：为什么并行下载数调大以后，速度数字变了？**

几个 IP 同时下载会分同一条出口带宽，单 IP 的测量值就不再代表它的真实能力。
这只是「筛掉明显不通的 IP」的手段；要横向比较快慢，把并行数设成 1。

**Q：定时到点了没跑？**

- 看顶部有没有「下次 XX:XX」；没有的话是定时被关了或表达式不合法
- `docker exec cf-optimizer cat /etc/crontabs/root` 看实际写入的规则
- `docker logs --tail 50 cf-optimizer` 看有没有 `/run.sh` 的输出
- 容器重启不会丢设置（会从 `/data/web-config.json` 重新读）

**Q：面板密码忘了 / 想换面板？**

网页左栏「CFNext 面板」里重填一遍，密码**留空表示不修改**。真忘了就删掉
`/data/web-config.json` 里的 `panel_password` 字段，或者整个文件（配置会回到默认值）。

**Q：支持 IPv6 吗？**

支持。IPv6 输出会自动加方括号（`[2606:4700::1]:443#LAX`）。

**Q：备注里有中文，推送会不会乱码？**

不会。推送前会把结果转成 JSON，中文和 `|` 都做过转义验证。

**Q：8090 端口被占用？**

改 compose 里的 `WEB_PORT` 和 `ports` 映射，两处都要改。

---

## 发布自己的镜像（Docker Hub）

想把镜像发到 Docker Hub，让别人直接 `docker pull` 用，有两种方式。**推荐用 GitHub Actions**，
因为 Docker Hub 免费账号已经取消了「自动化构建（Automated Builds）」，只能在本地或 CI 里构建后 push。

### 方式一：GitHub Actions 自动构建（推荐）

推一次 tag，CI 自动构建 amd64 + arm64 双架构并推送到 Docker Hub 和 GHCR。

#### 0. 前置：代码已经在 GitHub 上

如果还没有仓库：

```bash
git init
git add .
git commit -m "init"
git branch -M main

# 在 github.com 上 New repository 建一个空仓库（不要勾 README / .gitignore，否则会冲突）
git remote add origin https://github.com/<你的GitHub用户名>/cf-optimizer.git
git push -u origin main
```

> 推之前确认 `.gitignore` 生效：`git status` 里**不应该**出现 `data/`、`out/`、`.env`、`cfst`。
> 这几个都含敏感信息或几十 MB 的二进制。用 `git ls-files | grep -E "^(data/|out/|\.env$|cfst$)"` 检查，有输出就说明漏了。

#### 1. 注册 Docker Hub 并建仓库

没有账号先去 [hub.docker.com](https://hub.docker.com) 注册（免费）。然后：

Create repository → **Name** 填 `cf-optimizer` → **Visibility** 选 **Public** → Create。

仓库名必须和 workflow 里的 `/cf-optimizer` 一致，否则 push 会被拒。

#### 2. 生成 Access Token

[Account Settings → Security → New Access Token](https://hub.docker.com/settings/security)，
描述随便填，权限选 **Read & Write**。**保存好，只显示一次**。

> ⚠️ 不要用登录密码。Docker Hub 2021 年后已不支持密码认证，必须用 Token。

#### 3. 在 GitHub 仓库里加两个 Secret

仓库 → Settings → Secrets and variables → Actions → **New repository secret**：

| Name | Value |
|---|---|
| `DOCKERHUB_USERNAME` | 你的 Docker Hub 用户名（小写） |
| `DOCKERHUB_TOKEN` | 上一步生成的 Access Token |

#### 4. 打 tag 推送

```bash
git tag v1.0.0
git push origin v1.0.0
```

去仓库的 **Actions** 页面看进度（约 5~10 分钟，主要花在 arm64 的 QEMU 模拟上）。
完成后镜像就有了：

```bash
docker pull <你的用户名>/cf-optimizer:latest
docker pull <你的用户名>/cf-optimizer:1.0.0
docker buildx imagetools inspect <你的用户名>/cf-optimizer:1.0.0   # 看是否双架构
```

同时也会推到 `ghcr.io/<你的GitHub用户名>/cf-optimizer`（用内置 `GITHUB_TOKEN`，不用额外配置）。

#### 5. 改掉占位符

发布前把这几处的 `YOUR_DOCKERHUB_USER` / `YOUR_GITHUB_USER` 换成你的（workflow 里用的是 Secret，
不用改）：

```bash
grep -rn "YOUR_DOCKERHUB_USER\|YOUR_GITHUB_USER" README.md docker-compose.yml Dockerfile
```

改完再提交一次，徽章和 `docker pull` 命令就都对了。

### 方式二：本地构建推送

不想配 CI，就在一台能上网的机器上直接推（脚本已经写好了）：

```bash
docker login -u <你的用户名>          # 密码处填 Access Token

chmod +x publish.sh
./publish.sh -u <你的用户名> -p 1.0.0        # 只构建本机架构，快
./publish.sh -u <你的用户名> -p 1.0.0 -m     # 构建 amd64 + arm64，慢但通用
```

`-m` 需要 QEMU 模拟 arm64，脚本检测到没有会自动装。构建 arm64 时 CloudflareST 会下载
`cfst_linux_arm64.tar.gz`，Dockerfile 里按 `TARGETARCH` 自动选包，不会装错架构。

### 关于多架构

`Dockerfile` 用 buildx 注入的 `TARGETARCH` 决定下载哪个架构的 CloudflareST：

| 平台 | 下载的包 |
|---|---|
| `linux/amd64` | `cfst_linux_amd64.tar.gz` |
| `linux/arm64` | `cfst_linux_arm64.tar.gz` |
| `linux/arm/v7` | `cfst_linux_armv7.tar.gz` |

默认只构建 amd64 + arm64（覆盖绝大多数 NAS 和服务器）。要兼容 ARMv7 老设备，
把 workflow 里的 `platforms` 改成 `linux/amd64,linux/arm64,linux/arm/v7`。

如果目标架构不在支持列表里，构建会直接报错退出，而不是装一个跑不起来的二进制
——**前提是你在构建上下文里没放本地 `cfst`**，放了的话会无条件使用它。

### 构建加速

- CI 里用了 `cache-from/to: type=gha`，第二次构建基本只跑改动层
- 本地构建国内网络慢，加 `--build-arg HTTPS_PROXY=http://<代理>:7890` 下载 CloudflareST
- 想完全离线：把 `cfst` 二进制放到构建目录（`.dockerignore` 里默认**没有**排除它，会一起打包）

---

## 更新 / 卸载

用现成镜像时：

```bash
docker compose pull && docker compose up -d      # 更新到最新版
docker compose down                              # 停止并删除（data 和 out 保留）
docker compose down && rm -rf data out           # 彻底清理（含配置，慎用）
```

自己构建时：

```bash
docker compose up -d --build    # 更新代码后重建
docker compose logs -f --tail 100
```

---

## 免责声明

本项目仅用于学习与研究 Cloudflare 网络质量。请遵守所在地区的法律法规及 Cloudflare 的服务条款，
不要用于任何违法用途，作者不对使用本程序产生的后果承担责任。

CloudflareST 版权归 [XIU2](https://github.com/XIU2/CloudflareSpeedTest) 所有；
CFNext 版权归 [PAICNI](https://github.com/PAICNI/CFNext) 所有。

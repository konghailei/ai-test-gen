# 内网部署与迁移手册（Kernel 3.10 / 4.x 双兼容）

> 版本：v4.1.6 ｜ 适用：RHEL/CentOS 7 系（内核 3.10）、内核 4.x 及以上
> 面向：**无法访问外网、无法在线装包**的内网服务器
> 最后更新：2026-10-27
>
> **本文是内网迁移的唯一入口文档**，覆盖：
> ① 宿主配置清单（照着敲）；② 每项为什么必须做；③ 怎么验证做对了；
> ④ 迁移版本记录（**每次迁移必须追加，见第二节**）。
>
> 应用侧的 3.10 兼容已内置在代码里（`resource_guard` 的 cgroup v1 分支），
> **不需要改代码、不需要重新打包**，只做宿主配置 + 部署步骤即可。
>
> 关联文档：`docs/07-internal-deployment.md`（更详细的架构/备份/加固）、
> `docs/28-内网部署更新包记录.md`（v3.5.9 单次更新包记录，历史归档）。

---

## 〇、迁移前 30 秒自检

每次迁移**开干之前**先回答这四个问题，答不上来就别开始：

| # | 问题 | 怎么查 |
|---|---|---|
| 1 | 目标机器内核是多少？cgroup 是 v1 还是 v2？ | `uname -r`；`ls /sys/fs/cgroup/cgroup.controllers 2>/dev/null && echo v2 \|\| echo v1` |
| 2 | 这台机器之前装过吗？装的什么版本？ | `cat /opt/<安装目录>/.deployed-version`（或看 `DEPLOY-RECORD.md`） |
| 3 | 这次是升级还是回滚？包版本是多少？ | `head -1 <包目录>/VERSION` |
| 4 | 宿主配置（第二节 5 项）做过吗？ | 按第二节逐项验证命令过一遍 |

> 问题 2 的答案直接决定"要不要重新做宿主配置"：**首次迁移必须全做；升级迁移只需验证**。

---

## 〇之二、迁移版本记录（**每次迁移必须追加一行**）

> 规则：每次内网迁移完成后，由执行人**立刻**在下表追加一行。
> 这张表是事后唯一能回答"这台机器现在跑什么版本、当时怎么部署的"的记录，
> 漏记一次，下次排查就全凭记忆。
>
> `deploy.sh` 会自动在安装目录写 `DEPLOY-RECORD.md`（含同一批字段），
> 可以把那个文件里的值抄过来——不需要你重新敲命令采集。

| 日期 | 包版本 | 类型 | 内核 | cgroup | mem_source | cpu_source | 并发上限 | 结果 | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-27 | v4.1.5 | 开发机预演 | 3.6.9(容器内) | v2 | `cgroup_v2` | `cgroup_v2` | 2 | ✅ 通过 | 小精灵 | 16GB/8核 → api 10g·6核 / celery 4g / milvus 6g。修复 D1-D5 部署缺陷后预演 |
| 2026-10-28 | v4.1.5 | 开发机复跑 | 3.6.9(容器内) | v2 | `cgroup_v2` | `cgroup_v2` | 1 | ✅ 通过 | 小精灵 | 同上机型。验投影链路：celery 占 3 核 → api 可用 3 核 → ceiling 2→1。修 C7-C20 后复跑 |
| 2026-10-28 | v4.1.6 | 开发机复跑 | 3.6.9(容器内) | v2 | `cgroup_v2` | `cgroup_v2` | 1 | ✅ 通过 | 小精灵 | 同上。**新增 C17/C18 两个静默中断修复**（netstat stub 静默退出、无 code/ 时镜像不重建），验收 554 passed |

> **上面这一行是"预演"不是"真迁移"**：在开发机（Windows + Docker Desktop）上验证了
> 配额生成、cgroup 读取、版本判定、`INSTALL_DIR` 分离部署四条链路。
> 真内网机器的第一次迁移请自行追加一行，不要覆盖这行——预演记录也有参考价值
> （比如它证明了 `mem_source` 从 `proc` 变成 `cgroup_v2` 的完整因果）。

**字段填写说明：**

| 字段 | 取值来源 | 说明 |
|---|---|---|
| 包版本 | `head -1 <包目录>/VERSION` | 与 `backend/app/VERSION` 不一致时在备注里写明 |
| 类型 | 首次 / 升级 / 回滚 | 回滚必须写清"从 X 回到 Y" |
| 内核 | `uname -r` | 只写主次版本也行（如 3.10.0） |
| cgroup | `v1` / `v2` | 由 `/sys/fs/cgroup/cgroup.controllers` 是否存在判定 |
| mem_source | `/api/health` 的 `generation_load.mem_source` | **`proc` = OOM 保护失效，必须处理后再记"通过"** |
| cpu_source | 同上 `cpu_source` | `cpu_count` = 未受配额约束，属告警项 |
| 并发上限 | 同上 `task_ceiling` | 便于横向对比不同机器的容量。**同时记下 `cpu_topology`**：`heuristic` 说明物理核是 ÷2 猜的（见第五之二节） |
| 结果 | ✅ 通过 / ⚠️ 通过有告警 / ❌ 失败 | 有告警时在备注写清是什么 |
| 备注 | 宿主机规格、踩到的坑、临时处置 | 未来排障最有用的一列，别留空 |

**没有这张表会怎样（真实代价）：** 下次迁移时无法判断"上次是不是做过 `vm.max_map_count`"
"上次那台 3.10 的机器 OOM 保护到底生效没"，只能重新验一遍，或者赌运气。

---

## 一、先分清"内核版本"与"cgroup 版本"

这是最容易踩的概念混淆，先把它拆开：

| | 内核 3.10（CentOS 7） | 内核 4.0 ~ 5.7 | 内核 5.8+ |
|---|---|---|---|
| cgroup 默认 | **v1** | v1（v2 可选） | **v2** |
| `memory.limit_in_bytes` | ✅ 有 | ✅ 有 | ❌ 无（用 `memory.max`） |
| `memory.max` | ❌ 无 | ❌ 无 | ✅ 有 |
| `cpu.cfs_quota_us` | ✅ 有 | ✅ 有 | ❌ 无（用 `cpu.max`） |
| nf_tables | ❌ 不支持 | ✅ | ✅ |
| `clone3` syscall | ❌ 没有 | ✅ | ✅ |

**关键结论：真正决定应用程序行为的是 cgroup 版本，不是内核版本号。**
内核 4.4 的 CentOS 7 和内核 3.10 的 CentOS 7 行为完全一致（都是 cgroup v1）；
内核 5.10 的 Ubuntu 22.04 才是 cgroup v2。

应用代码对两条路径都支持：
- cgroup v2 → 读 `memory.max` / `memory.current` / `cpu.max`
- cgroup v1 → 读 `memory.limit_in_bytes` / `memory.usage_in_bytes` / `cpu.cfs_quota_us`
- 都没有 → 退到 `/proc/meminfo`（**这是危险降级**，见第三节）

---

## 二、宿主配置清单（3.10 必做 5 项，4.x 必做 1 项）

> 全部需要 root。做完每一项都有验证命令，**别跳过验证**——
> 其中 2 项配置失败时不会报错，只在特定场景才暴露（那时已经出事）。

### 第 1 项：`vm.max_map_count`（所有版本必做）

Milvus 官方硬要求。CentOS 7 默认 `65530`，数据量上来后 Milvus 直接崩溃，报
`max virtual memory areas vm.max_map_count too low`。

```bash
# 临时生效
sysctl -w vm.max_map_count=262144

# 永久生效（推荐用 sysctl.d，比改 /etc/sysctl.conf 更干净）
cat > /etc/sysctl.d/99-ai-test-gen.conf <<'EOF'
vm.max_map_count = 262144
EOF
sysctl --system
```

**验证：**
```bash
cat /proc/sys/vm/max_map_count     # 必须 >= 262144
```

> `deploy.sh` 会检查这项，root 时会尝试自动 `sysctl -w`。
> 但**持久化必须你手动做**——脚本不该自动写宿主的 `/etc/sysctl.d/`。

---

### 第 2 项：Docker 的 `LimitMEMLOCK=infinity`（3.10 必做）

**这一项最容易漏，漏了之后 Milvus 能启动，但数据量上来会崩。**

原因：`docker-compose.offline.yml` 里给 milvus 声明了 `ulimits.memlock: -1`，
但这个配置只有在 **Docker daemon 本身允许 memlock** 时才生效。
CentOS 7 的 systemd 默认给 docker 服务设了 memlock 限制，会静默截断容器配置。

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/limits.conf <<'EOF'
[Service]
LimitMEMLOCK=infinity
LimitNOFILE=1048576
EOF

systemctl daemon-reload
systemctl restart docker
```

**验证（必须看容器内的值，不是宿主的值）：**
```bash
# 部署后执行
docker exec ai-test-gen-milvus sh -c 'ulimit -l'
# 期望输出：unlimited
# 如果输出 64 或 8192 → 上面哪一步没生效
```

> ⚠️ `systemctl restart docker` 会重启所有容器。请在部署**前**做，或接受中断。

---

### 第 3 项：iptables 后端必须是 legacy（3.10 必做）

3.10 内核不支持 nf_tables。如果系统被配成 `iptables-nft`，Docker 的端口映射会**静默失效**——
容器能起来、`docker ps` 显示正常，但外网连不上，也没有明确报错。

```bash
# 查看
iptables --version
# 输出示例：iptables v1.8.4 (nf_tables)  ← 括号里是后端

# 如果是 nf_tables，切换：
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
systemctl restart docker
```

**验证：**
```bash
iptables --version | grep -o '(.*)'      # 必须是 (legacy)
docker run --rm -p 18080:80 nginx:alpine  # 能有端口映射说明 OK（可跳过）
```

> 没有 `iptables-legacy` 文件？那是精简安装，用
> `yum install -y iptables-services` 或从同版本系统里拷二进制。

---

### 第 4 项：runc ≥ 1.1.0（3.10 必查）

3.10 内核没有 `clone3` syscall。旧版 runc（Docker 20.10.x 自带的 1.0.x）在新版容器配置下会报：

```
runc: clone3: operation not permitted
```

表现：容器起不来，或者起一部分就全部失败。

```bash
docker info | grep -i -E "runc|kernel"
runc --version      # 需要 >= 1.1.0
```

**如果低于 1.1.0：**
- 首选：升级 Docker 到 20.10.10+（这些版本捆绑的 runc 已修）
- 备选：单独下载 `runc` 二进制替换 `/usr/bin/runc`（内网需离线包）

> 内网无法下载的话——如果你当前 Docker 能正常跑容器，说明现版本可用，**不要动**。
> 这项只在"新装机器起不来容器"时需要排查。

---

### 第 5 项：磁盘与 inotify（3.10 建议）

```bash
# 磁盘：首次部署要 30GB+，建议 50GB
df -h /var/lib/docker

# inotify：文件监听上限，默认值在大量容器下会不够
cat /proc/sys/fs/inotify/max_user_watches         # 建议 >= 524288
sysctl -w fs.inotify.max_user_watches=524288
echo 'fs.inotify.max_user_watches = 524288' >> /etc/sysctl.d/99-ai-test-gen.conf
```

---

### 4.x 内核（≥ 4.0）需要做什么

| 项 | 是否需要 | 说明 |
|---|---|---|
| `vm.max_map_count` | ✅ **必做** | 与内核版本无关，Milvus 硬要求 |
| `LimitMEMLOCK` | 建议 | 4.x 上 systemd 默认限制更宽松，但做了更保险 |
| iptables 后端 | ❌ 不需要 | 4.x 支持 nf_tables |
| runc 版本 | ❌ 不需要 | 4.x 有 `clone3` |
| inotify | 建议 | 同 3.10 |

**内核 5.8+ 的用户额外注意**：cgroup v2 下如果完全没设 `--memory`，
`memory.max` 是 `max`，应用同样会降级到读宿主内存。**所以 override 配额文件同样必须生效**（见第三节）。

---

## 三、一页配置表（复制即用）

### 3.1 宿主机 `/etc/sysctl.d/99-ai-test-gen.conf`

```ini
# AI-Test-Gen 内网部署所需内核参数
# 适用：Kernel 3.10 / 4.x / 5.x 全部

# Milvus 硬要求（默认 65530 不够）
vm.max_map_count = 262144

# 文件监听（容器较多时）
fs.inotify.max_user_watches = 524288
```

应用方式：
```bash
sysctl --system
```

### 3.2 Docker daemon `/etc/systemd/system/docker.service.d/limits.conf`

```ini
[Service]
# Milvus mmap 锁页需要；CentOS 7 默认值会截断容器的 ulimits.memlock
LimitMEMLOCK=infinity
LimitNOFILE=1048576
```

应用方式：
```bash
systemctl daemon-reload && systemctl restart docker
```

### 3.3 `install.env`（部署包内）

```ini
NGINX_PORT=80
API_PORT=8000
DEBUG=false
PG_PASSWORD=<改成你的强密码>
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=<改成强密码>

# ---- 容器资源配额（v4.1.5）----
# 留空 = deploy.sh 按宿主自动算（推荐，不需要你操心）
#  8GB 宿主 → api 6g·cpus 6 / celery 3g·cpus 3 / milvus 3g
# 16GB 宿主 → api 10g·cpus 6 / celery 4g·cpus 3 / milvus 6g
# 32GB 宿主 → api 20g·cpus 14 / celery 6g·cpus 7 / milvus 8g
#
# 想手工指定就取消注释：
# API_MEM_LIMIT=10g
# API_CPU_LIMIT=6
# MILVUS_MEM_LIMIT=6g
# CELERY_MEM_LIMIT=4g
# CELERY_CPU_LIMIT=3
```

> **为什么这一节很关键**：容器不设内存上限时，应用的 OOM 保护（内存紧张自动降并发）
> 读的是**宿主内存**而不是容器上限，判定与实际风险无关 → 保护等于没有。
> 且 `mem_limit` / `cpus` 在 compose 里不接受空值，所以配额靠
> `docker-compose.resources.override.yml` 注入（deploy.sh 自动生成，不要手改）。
>
> **三个服务都要限额（内存 + CPU 都要）**，各有各的理由：
> - `backend`：OOM 保护与并发预算的依据（读 container cgroup 上限）
> - `celery_worker`：独立进程，各自加载一份 bge + reranker，没有降并发守卫，
>   不限额会和 backend 抢内存，且 OOM Killer 挑谁全看运气（3.10 常杀 PID 最大者）
> - `milvus`：内存随集合数量上涨，没有守卫，限它主要是兜住别把整机吃干
>
> **celery 的 CPU 为什么也必须限**（v4.1.5 补）：它跑 embedding + 双写，
> 每个 worker 进程各有一份 torch 线程池（4 线程）。不设 `cpus` 时它看到全部宿主核、
> 与 API 争同一批物理核，而应用侧的 `task_ceiling` **完全看不到它**
> （那是另一个进程，预算只管 API 自己的生成任务）。
> 实测形态：api 限 6 核、celery 看到 8 核 → 两边合计最多开 6 + 2×4 个线程池，
> 远超宿主 8 逻辑核容量。故默认取 api 的一半（下限 1）：
> `10g→cpus 5`、`6g→3`、`4g→2→1`。可显式设 `CELERY_CPU_LIMIT` 覆盖。

---

## 四、部署流程（内网实际执行顺序）

### 顺序很重要：宿主配置 → 重启 Docker → 再部署

```bash
# ===== 第 0 步：宿主配置（只做一次，需 root）=====
sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count = 262144' > /etc/sysctl.d/99-ai-test-gen.conf
sysctl --system

mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/limits.conf <<'EOF'
[Service]
LimitMEMLOCK=infinity
LimitNOFILE=1048576
EOF
systemctl daemon-reload
systemctl restart docker

# 3.10 额外：确认 iptables 后端
iptables --version        # 括号内应为 legacy

# ===== 第 1 步：解压离线包 =====
tar -xzf ai-test-gen-offline-*.tar.gz    # 或 unzip
cd ai-test-gen-offline/

# ===== 第 2 步：核对版本（别拷错包）=====
head -1 VERSION                        # 包版本
head -1 backend/app/VERSION            # 代码版本，两者应一致

# ===== 第 3 步：改 install.env =====
vi install.env          # 至少改 PG_PASSWORD / MINIO_SECRET_KEY

# ===== 第 4 步：部署 =====
bash deploy.sh
```

`deploy.sh` 会自动完成：**版本核对** → 环境预检（内核/cgroup/内存/磁盘/iptables）→ 加载镜像 →
复制模型 → 建立数据目录 → **生成资源配额 override** → 启动 → 健康检查 →
**OOM/CPU 配额自检** → **写 `DEPLOY-RECORD.md`**。

### 4.1 首次迁移 vs 升级迁移

| | 首次迁移 | 升级迁移（增量包） |
|---|---|---|
| 包类型 | `ai-testgen-offline-pack-vX.Y.Z`（含 images/ models/，~6.5GB） | `ai-testgen-offline-pack-update-vX.Y.Z`（~5MB） |
| 宿主配置 | **5 项全做** | 只需验证（第二节验证命令） |
| `install.env` | 从模板改（改密码） | 会被智能合并，**已有值不覆盖**——密码不会丢 |
| 模型文件 | 需要（包内自带） | 不需要 |
| base 镜像 | 需要（包内自带） | 复用已有，只重建应用层 |
| 耗时 | 20~40 分钟 | 3~8 分钟（含前端镜像加载） |

> **升级时 `deploy.sh` 会保留你的 `.env`**：模板里新增的变量追加进去，
> 已有的 `PG_PASSWORD` / `MINIO_SECRET_KEY` 等**不会被覆盖**。
> 但也因此——如果新版需要改某些默认值（如新增配额变量），**不会自动生效**，需手工确认。

### 4.2 部署时的版本保护

`deploy.sh` 启动时会打印三方版本并落盘：

```
[INFO] 版本信息:
[INFO]   离线包版本 : v4.1.5
[INFO]   代码版本   : v4.1.5
[INFO]   已装版本   : v4.1.4
[INFO]   → 升级部署：v4.1.4 → v4.1.5
```

三种异常会明确告警（**不阻断**，因为回滚是合法操作）：

| 现象 | 含义 | 该怎么办 |
|---|---|---|
| `⚠️ 这是降级部署：包版本 v4.1.3 低于已装版本 v4.1.5` | 拷错了包，或有意回滚 | 有意的继续；拷错了立刻 Ctrl-C |
| `⚠️ 包版本(v4.1.5) 与代码版本(v4.1.4) 不一致` | 打包后又改了代码，前端镜像是旧的 | 重新打包（打包脚本会做一致性校验） |
| 部署结束提示 `未标注，可能是源码目录直跑` | 包内没有 `VERSION` 文件 | 用打包脚本生成的包，别直接拷源码目录 |

**部署记录文件**：结束后安装目录下会多两个文件

```
.deployed-version   # 一行版本号，deploy.sh 下次启动读它做降级判断
DEPLOY-RECORD.md    # 完整快照：版本/内核/cgroup/mem_source/cpu_source/启动命令
```

> 内网机器通常不能上网、查不到聊天记录，**现场唯一能确认"这机器什么时候、
> 用什么包、在什么内核上部署的"就是 `DEPLOY-RECORD.md`**。迁移完请顺手把它
> 的关键字段抄到第二节的迁移记录表里。

### 4.3 后续手工操作必须带 override

`deploy.sh` 在启动时会叠加 `-f docker-compose.resources.override.yml`。
**你手工执行 compose 命令时也要带上**，否则配额丢失：

```bash
# ✅ 正确
docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml ps
docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml logs -f

# ❌ 错误：这样会丢掉内存/CPU 上限，OOM 保护失效
docker compose -f docker-compose.offline.yml restart
```

嫌麻烦可以写进 shell alias：
```bash
# 加到 ~/.bashrc
alias atg='docker compose -f /opt/ai-test-gen/docker-compose.offline.yml -f /opt/ai-test-gen/docker-compose.resources.override.yml'
# 之后：atg ps / atg logs -f / atg restart
```

---

## 五、验证清单（部署后逐项打勾）

### 5.1 内核与 cgroup 判定

```bash
uname -r                                  # 3.10.x 或 4.x+
ls /sys/fs/cgroup/cgroup.controllers 2>/dev/null && echo "cgroup v2" || echo "cgroup v1"
```

### 5.2 关键参数

```bash
cat /proc/sys/vm/max_map_count            # >= 262144
iptables --version | grep -o '(.*)'       # (legacy) —— 仅 3.10 需要
docker exec ai-test-gen-milvus sh -c 'ulimit -l'   # unlimited
```

### 5.3 应用侧：OOM 保护是否真的有效（**最重要**）

```bash
curl -s http://127.0.0.1:8000/api/health | python -m json.tool | grep -E "mem_source|cpu_source|task_ceiling|mem_available_ratio"
```

期望（3.10 上）：

```json
"mem_source": "cgroup_v1",
"cpu_source": "cgroup_v1",
"task_ceiling": 1,
"mem_available_ratio": 0.87
```

期望（4.x / 5.x 上）：

```json
"mem_source": "cgroup_v2",
"cpu_source": "cgroup_v2"
```

**判定规则：**

| 值 | 含义 | 处理 |
|---|---|---|
| `mem_source: cgroup_v1` | ✅ 3.10 兼容路径生效，读到容器上限 | 无需处理 |
| `mem_source: cgroup_v2` | ✅ 4.x/5.x 路径生效 | 无需处理 |
| `mem_source: proc` | ❌ **读的是宿主内存，OOM 保护不生效** | 见下 |
| `cpu_source: cpu_count` | ⚠️ 应用看到宿主全部核，并发预算被放大 | 见下 |

**`mem_source: proc` 的排查顺序：**
1. `ls -l docker-compose.resources.override.yml` —— 文件存在吗？
2. `docker inspect ai-test-gen-api --format '{{.HostConfig.Memory}}'` —— 输出非 0 才算设上了
3. 输出非 0 但还是 `proc` → 代码没更新，重跑 `deploy.sh`（会重建应用镜像）
4. 输出是 0 → override 没被叠加。

> ⚠️ **第 3 步和第 4 步的顺序反了会白折腾**（2026-10-27 实测踩到）：
> `docker compose up -d` **不会因为 override 新增 `mem_limit` 而重建已有容器**——
> 它会认为"配置没变"，容器保持原样。必须显式加 `--force-recreate`：
>
> ```bash
> docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml \
>   up -d --force-recreate --no-deps backend
> ```
>
> 只改配额（不动代码）时用这条；`deploy.sh` 全量部署时会自己重建，无需手动。

### 5.4 三个容器的配额都要核对

```bash
for c in ai-test-gen-api ai-test-gen-celery ai-test-gen-milvus; do
  printf '%-22s ' "$c"
  docker inspect "$c" --format 'mem={{.HostConfig.Memory}}'
done
```

期望：三个都不是 0。

```
ai-test-gen-api        mem=10737418240    (10g)
ai-test-gen-celery     mem=4294967296     (4g)
ai-test-gen-milvus     mem=6442450944     (6g)
```

> celery 只看 `mem`，它没有 CPU 配额（CPU 侧读到宿主全部核属**正常**——
> 它只跑嵌入/双写，不需要 CPU 配额；`cpu_source: cpu_count` 的告警只针对 backend）。

### 5.5 容器健康

```bash
docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml ps
# 8 个服务全部 Up (healthy)
```

### 5.6 一轮功能验证

```bash
# --noproxy '*' 别省：机器上若有 http_proxy 环境变量，
# 访问 127.0.0.1 会被代理接管 → 返回空/HTTP 000，看起来像"服务挂了"，实际是走错路
curl -s --noproxy '*' http://127.0.0.1:8000/api/health

curl -s --noproxy '*' -X POST http://127.0.0.1:8000/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"<你的密码>"}' | head -c 200
```

> 若 `curl` 仍失败但 `docker exec ai-test-gen-api` 能通，说明是宿主网络/代理问题，
> 不是应用问题——可直接用容器内探针：
> ```bash
> docker exec ai-test-gen-api python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/health').status)"
> ```

### 5.7 迁移收尾（**别忘**）

```bash
# 1. 查看自动生成的部署记录
cat DEPLOY-RECORD.md

# 2. 把关键字段抄进 docs/24 第二节的「迁移版本记录」表
#    至少填：日期 / 包版本 / 内核 / cgroup / mem_source / cpu_source / task_ceiling / 结果

# 3. 备份（升级前若有业务数据，这一步应该已在前面做过）
tar czf ../backup-$(date +%F)-$(head -1 .deployed-version).tar.gz \
    data/pgdata data/.keys milvus/ 2>/dev/null
```

> 第 2 步不是形式主义：**下次迁移判断"要不要重做宿主配置"就靠这张表**。
> 漏记后唯一能做的就是从零验证一遍，白花 20 分钟。

---

## 五之二、CPU 核数口径：并发按物理核，线程按逻辑核（v4.1.5 定案）

迁移到内网后如果发现「机器明明是 16 核，却只让跑 2 个生成任务」，看这一节。

### 5-2.1 两种核数，两个不同用途

| 量 | 怎么得到 | 用来定什么 | 为什么 |
|---|---|---|---|
| **逻辑核**（`cpu_limit()`） | cgroup 配额 `cpu.max` / `cpu.cfs_quota_us` → `sched_getaffinity` → cpuset → `cpu_count` | ① 单次推理的 torch 线程数上限 ② 负载降级的比较基准 | 逻辑核是**调度单位**：时间片、可运行队列都按它算 |
| **物理核**（`physical_cores()`） | 读 `/proc/cpuinfo` 的 `siblings / cpu cores` 得每核线程数，再 `ceil(可用逻辑核 / 每核线程数)` | 全局并发任务数（`inference_slots`） | 超线程对 transformer matmul 是**纯竞争**，实测 8 线程比 4 线程慢 22%（docs/27 §2.6）→ 并发粒度必须落在物理核上 |

一句话：**开几个任务看物理核，每个任务开几个线程看逻辑核（且封顶 4）。**

### 5-2.2 为什么并发不能按逻辑核算

本机实测（i7-1165G7，4 核 8 线程，api 容器 `cpus=6`）：

```
8 个逻辑核 = 4 个物理核 × 2 线程
容器配额 6 逻辑核 → 拿到 3 个"整物理核"的算力
   若按逻辑核 6 定并发 → 6 个任务同时 encode，每个 4 线程 = 24 线程抢 6 核
   → 上下文切换吃掉全部收益，且事件循环被拖到秒级（就是 S1 那种卡顿）
   按物理核 3 定并发 → inference_slots = 2（留 1 核给主进程）
```

### 5-2.3 为什么不能再"逻辑核 ÷ 2"

旧实现 `physical_cores = cpu_limit // 2` 是**硬编码假设"一定开了超线程"**：

| 机器形态 | 逻辑核 | ÷2 会报 | 真实物理核 | 后果 |
|---|---|---|---|---|
| 4 核 × 2 线程（本机） | 8 | 4 | 4 | ✅ 碰巧对 |
| 关闭超线程，8 核 | 8 | 4 | **8** | ❌ 并发预算砍半 |
| `--cpus=6`（非偶数份额） | 6 | 3 | 3 | ✅ 碰巧对 |
| `--cpus=3` | 3 | 1 | 3 | ❌ 预算砍到 1/3 |
| ARM 大核簇 / 异构核 | - | - | - | ❌ 模型不成立 |

v4.1.5 起改为**真读拓扑**，读不到才退回 ÷2 保守估计（宁少不多）。
`/api/health` 的 `generation_load.cpu_topology` 会明确告诉你走的是哪条：

- `cpuinfo` = 读到真实拓扑并通过一致性校验（推荐，正常内网机器都是这个）
- `heuristic` = 拓扑**读不到或自相矛盾**，按 ÷2 猜（此时 `cpu_physical_cores` 可能是错的）

> **一致性校验做了什么**（防止"读到脏数据比读不到更糟"）：
> 有些 ARM / 虚拟机把 `cpu cores` 填成整机总核数，若照单全收会算出
> `tpc=1 → 物理核=逻辑核 → 并发预算翻倍`。故采信前必须过三条：
> ① `tpc ∈ {1,2,4,8}`（SMT 现实取值，3/5/6 不可能）；
> ② `逻辑处理器数 % siblings == 0`（每 socket 线程数必须整除总数）；
> ③ `逻辑处理器数 / siblings == physical id 个数`（socket 数要对得上）。
>
> 注意：**关超线程的机器（`siblings == cores`）是合法的**，三条能过就采信，
> 此时物理核=逻辑核是**正确**的，不是 bug。

> 若显示 `heuristic` 而又希望按物理核精确分配，检查容器能不能读 `/proc/cpuinfo`：
> `docker exec ai-test-gen-api grep -c "^processor" /proc/cpuinfo`（应为逻辑核数）。
> 能读到却是 `heuristic` → 字段自相矛盾，按下面命令看具体值：
> `docker exec ai-test-gen-api grep -E "^(processor|siblings|cpu cores|physical id)" /proc/cpuinfo | sort | uniq -c`

### 5-2.4 单次推理的线程数：torch 默认值是错的

`torch.set_num_threads()` 的默认值是**宿主物理核数**，与容器的 cgroup 配额**无关**。
容器被限到 6 逻辑核时，torch 仍可能开满宿主全部硬件线程。

v4.1.5 前只有**去重进程池的 worker init** 里设了 4 线程，
**API 主进程从头到尾没设过** —— 于是"并发预算算得再准"也会被单个 encode 打穿。

现在启动时统一设（`main.py` lifespan → `apply_torch_threads()`）：

```
encode_threads = min(4, 物理核, 逻辑核)
   4   = 单次 encode 的实测最优粒度（docs/27 §2.6：4 线程 35.2s vs 8 线程 42.9s）
   物理核 / 逻辑核 双封顶 = 小机器（2 核容器）不会自伤
```

同时把 `interop` 线程池压到 1、`TOKENIZERS_PARALLELISM=false`
（单流推理场景下多开只会与 intra-op 抢核）。

### 5-2.5 负载降级的比较基准

`task_ceiling()` 在 CPU ≥ 85% 时会降一档，判据是 `load_1m > 比较基准`。

**基准必须是逻辑核**：Linux 的可运行队列按**逻辑 CPU** 统计（超线程的两个硬件线程
各算一个 CPU）。v4.1.5 前这里误用了物理核 —— 8 逻辑核 / 4 物理核的机器上
`load_1m=4.5`（属正常）就会被判"过载"，叠加 cpu 89% 后把预算无故砍一档。
实测现象就是"8 核机器只肯跑 2 个任务"。

### 5-2.6 内网迁移后怎么核对这一项

```bash
# 一条命令看全（在部署目录执行）
docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml \
  exec -T backend python -c "
from app.core import resource_guard as rg
print('logical       =', rg.cpu_limit())
print('physical      =', rg.physical_cores())
print('threads/core  =', rg._threads_per_core())
print('topo_source   =', rg._topology_source())
print('slots         =', rg.inference_slots())
print('encode_threads=', rg.encode_threads())
print('ceiling       =', rg.task_ceiling())"
```

期望（16 物理核 / 32 逻辑核 / 不限超线程，api 给 14 逻辑核配额）：

```
logical       = 14.0
physical      = 7        ← ceil(14/2)
threads/core  = 2
topo_source   = cpuinfo
slots         = 6        ← 7-1，再被 GEN_TASK_CONCURRENCY_MAX=4 截到 4
encode_threads= 4
ceiling       = 4
```

判读要点：

| 现象 | 含义 | 怎么办 |
|---|---|---|
| `topo_source = heuristic` | 读不到 `/proc/cpuinfo`，按 ÷2 猜 | 正常也能跑；要精确就让它可读 |
| `logical` 远小于实际核数 | cgroup 配额设小了 | 调 `API_CPU_LIMIT` 后重跑 `apply-resource-limits.sh` |
| `logical = 宿主总核数`（且确实限制了 cpus） | 配额没生效 | `docker inspect` 看 `NanoCpus`；要 `--force-recreate` 才换配额 |
| `encode_threads = 逻辑核数` 且很大 | 旧版本（未设 torch 线程） | 升级到 v4.1.5+ |

---

## 六、3.10 上的已知限制（不是 bug，是物理约束）

| 限制 | 原因 | 应对 |
|---|---|---|
| Milvus 单机版内存峰值较高 | 3.10 机器通常是老硬件 | 内存 ≤ 8GB 时把 `MILVUS_MEM_LIMIT` 设 3g，并控制集合数量 |
| 去重进程池建议设 `DEDUP_EMBED_WORKERS=1` | 每个 worker 各加载一份 bge 模型（~1.6GB） | 8GB 机器在 `.env` 里显式设 1，否则 2 个 worker 会吃 3.2GB |
| 3.10 的 memcg OOM killer 优先杀 PID 最大 | 老内核的选择策略 | 同上，避免去重 worker 被优先杀（表现为 `BrokenProcessPool` 后静默退回串行，只是变慢） |
| 没有 cgroup v2 的 `memory.high` 软限 | 3.10 不支持 | 靠应用的 `MEM_LOW_RATIO` 守卫提前降并发来替代 |

---

## 七、回滚

配错了想回到"完全不限制"的旧行为：

```bash
cd /opt/ai-test-gen

# 方式一：删除 override，用主 compose 重启
rm -f docker-compose.resources.override.yml
docker compose -f docker-compose.offline.yml up -d --force-recreate backend milvus

# 方式二：脚本删除（等价）
bash scripts/apply-resource-limits.sh --remove
docker compose -f docker-compose.offline.yml up -d --force-recreate backend milvus
```

内核参数回滚：

```bash
rm -f /etc/sysctl.d/99-ai-test-gen.conf
sysctl --system
# Docker 的 limits.conf 一般不需要回滚
```

> ⚠️ 回滚 override 后 `mem_source` 会变成 `proc`，OOM 保护失效。
> 只在排查"是不是配额导致的问题"时临时这么做，排查完要恢复。

---

## 八、常见问题

**Q: 3.10 上 `/api/health` 显示 `mem_source: cgroup_v1`，但 `mem_available_ratio` 一直很低？**
A: cgroup v1 的可用内存是"上限 - 使用量（剔除可回收页缓存）"。Milvus 和 bge 模型
常驻会让基线使用量很高，这是正常的。看 `task_ceiling` 是否 ≥ 1 即可；
若长期被压到 1，说明内存分配偏紧，调大 `API_MEM_LIMIT`。

**Q: 部署时不设 `API_MEM_LIMIT` 行不行？**
A: 行，`deploy.sh` 会按宿主自动算。**但不要手工把 override 删掉还期待保护生效**——
没限额就没依据。

**Q: 3.10 上 Milvus 起不来，日志报 `max virtual memory areas` 相关？**
A: `vm.max_map_count` 没生效。`sysctl -w` 只是临时，重启会丢；确认
`/etc/sysctl.d/99-ai-test-gen.conf` 存在且执行过 `sysctl --system`。

**Q: 3.10 上容器能起但外网访问不到？**
A: 九成是 iptables-nft 后端（第 3 项）。`iptables --version` 看括号。

**Q: 4.x 内核上一切正常，还需要配 override 吗？**
A: 需要。cgroup 版本决定"能不能读到上限"，override 决定"有没有上限可读"——
两件事。不设配额，4.x 上同样会退化成 `proc`。

---

## 九、改动涉及的文件

### 9.1 应用代码（3.10 兼容本体）

| 文件 | 作用 |
|---|---|
| `backend/app/core/resource_guard.py` | cgroup v1 内存/CPU 读取、cpuset 兜底、`mem_source`/`cpu_source` 观测 |
| `backend/app/db/milvus_store.py` | `_physical_cores` 与 resource_guard 统一口径（原实现分叉） |
| `backend/tests/test_resource_guard.py` | 36 项，覆盖 v1 哨兵双向判定、working_set 扣减、cpuset 解析、两处口径一致 |
| `frontend/src/types/index.ts` | `generation_load` 增加 `mem_source` / `mem_available_ratio` 等可选字段 |

### 9.2 部署链（迁移能否一次成功取决于这里）

| 文件 | 作用 |
|---|---|
| `scripts/apply-resource-limits.sh` | **新增** 按宿主资源生成配额 override（backend / celery / milvus） |
| `scripts/deploy.sh` | 版本核对与降级保护；内核/cgroup 判定；`vm.max_map_count`/memlock/iptables 检查；阶段 4-2 生成配额；阶段 6-2 自检；阶段 7 写部署记录 |
| `scripts/pack-first.sh` | 写 `VERSION` + 版本一致性校验；打包 `scripts/apply-resource-limits.sh`；`install.env` 补配额说明 |
| `scripts/pack-update.sh` | 同上（增量包） |
| `docker-compose.offline.yml` / `docker-compose.yml` | 日志轮转（防写满磁盘）、milvus ulimits、redis `noeviction`（防静默丢任务）、配额改由 override 注入 |
| `.gitignore` | 忽略 `docker-compose.resources.override.yml`（环境产物，不同机器取值不同） |

### 9.3 本次修复的部署缺陷（v4.1.5，2026-10-27）

> 这几条都是"**会静默失效**"级别的——不报错、不中断，但保护措施根本没生效。
> 记录在此，避免以后重构时又踩回去。

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| D1 | `deploy.sh` 混用 `PACK_DIR` 与工作目录 | 用 `INSTALL_DIR` 分离部署时：配额脚本找不到（跳过）、前端 tar 恒找不到（前端停在旧版） | 统一用 `WORK_DIR`（`cd` 后的当前目录）做运行期路径基准，两处路径都做候选 |
| D2 | `is_first_deploy` 只看 `$PACK_DIR/images` | `INSTALL_DIR` 模式下判断失真 | 两个位置都看 |
| D3 | `pack-first.sh` 从不写 `VERSION` 文件 | 首次包部署时版本显示"未标注"，事后无法核对 | 与 `pack-update.sh` 对称写入，并加版本一致性校验 |
| D4 | 无版本降级保护 | 拷错包/误回滚无人知 | 三方版本对照（包/代码/已装）+ 降级告警 + `.deployed-version` |
| D5 | `celery_worker` 无内存上限 | 与 backend 抢内存，OOM Killer 随机杀一个（3.10 偏好杀最大 PID） | 配额脚本补 celery，按 api 档位折半 |

### 9.4 本次修复的 CPU 口径缺陷（v4.1.5，2026-10-28）

> 这一批的共同点：**并发预算算得再准，也被别的地方的错口径抵消**。
> 单看每一处都不报错，合起来就是"8 核机器只肯跑 2 个任务"。

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| C1 | `physical_cores = 逻辑核 // 2`，硬编码假设开了超线程 | 关超线程的 8 核机报 4（预算砍半）；`--cpus=3` 报 1 | 读 `/proc/cpuinfo` 的 `siblings / cpu cores` 算真实每核线程数，`ceil(逻辑核/每核线程数)`；读不到才退回 ÷2 |
| C1b | （C1 的加固，反向审查发现）原实现只读第一个 `siblings`/`cpu cores` 就 break，且不校验 | 部分 ARM/虚拟机把 `cpu cores` 填成整机总核数 → `tpc=1` → **物理核=逻辑核，并发预算翻倍**，比 ÷2 更危险；另：夹具只写 2 段 processor 时无法暴露此缺陷 | 采信前过三条一致性校验（`tpc∈{1,2,4,8}`、`总数%siblings==0`、`socket 数与 physical id 吻合`），不过则退回 ÷2 |
| C2 | API 主进程**从未**调用 `torch.set_num_threads()` | torch 默认 = 物理核数（与容器配额无关），单个 encode 就能开满宿主硬件线程，把并发预算打穿 | 启动时 `apply_torch_threads()`；`encode_threads = min(4, 物理核, 逻辑核)`；interop 压到 1 |
| C3 | 负载降级判据 `load_1m > physical_cores()` | `load` 的可运行队列按**逻辑** CPU 统计；8 逻辑核机上 load 4.5 就判"过载"，叠加 cpu 89% 后无故砍一档 | 比较基准改 `cpu_limit()`（逻辑核），与 load 同量纲 |
| C4 | `celery_worker` 无 CPU 配额，且**看不到** api 侧的并发预算 | celery 按 2 worker × 4 线程 + api 不限，合计线程池远超宿主容量；应用侧完全感知不到它 | 配额脚本补 `celery_worker.cpus = api 的一半`（下限 1），档位 10g→5 / 6g→3 / 4g→1 |
| C5 | `DEDUP_EMBED_WORKERS` 只在 dev compose 有 | 内网 offline 走默认 `""` = 串行，"dev 验证过的并行优化在内网从未生效"，且无日志提示 | offline 补同一环境变量（映射写法，与 `<<: *backend-env` 兼容） |
| C6 | `milvus_store._physical_cores` 自带一份实现 | 与 resource_guard 漂移（历史已发生一次） | 直接委托 `resource_guard.physical_cores()`，删掉本地的 ÷2 |
| C7 | 跨容器预算投影写进**进程内 dict** | `docker exec` 起的是**另一个进程**，写进去 uvicorn 主进程永远读不到 → 脚本打印"投影已生效"，预算根本没变（实测踩到） | 改「环境变量 `PEER_CELERY_CPU`（compose 注入）+ 落盘 `data/.peer_cpu_budget`」两条通道；env 优先 |
| C8 | 投影语义写成 `min(自身, 兄弟份额)` | 8 核机 api 限 8、celery 占 2 → min 得 2（预算被砍到 1/4），实际应为 6 | 改为**减法** `max(1, 自身 − 兄弟)` |
| C9 | 投影注入时机在 `up -d` **之后** | 主进程启动时还没有投影；且投影是静态事实只读一次 → 永远不生效 | 挪到阶段 5-3（`up -d` 之前）；阶段 6-2a 改为**从主进程读回来验证** |
| C10 | `docker compose build` 覆盖同名 tag 后 `up -d` 不重建容器 | 新代码不生效：新函数导入失败、8 容器全 healthy、脚本仍打印"部署完成"（实测踩到） | 新增 `CODE_REBUILT` 标志 → `up -d --force-recreate` |
| C11 | `command -v docker compose` 当判据 | `command -v` 只接受**单个命令名**，永远判定失败 → 错选 `docker compose`（带空格）→ `unknown shorthand flag: 'f'` | 抽 `detect_compose_cmd()`：真执行 `docker compose version` 判断 |
| C12 | `PACK_DIR` 取 `scripts/` 目录 | 从仓库直接跑 `bash scripts/deploy.sh` 时所有路径错位（配额脚本/镜像/compose 全找不到） | 按"同级或上一级是否有 `docker-compose.offline.yml`"判定 + 保留 `SCRIPT_DIR` |
| C13 | `version_gte` 用 `awk '{printf "%d%03d%03d"}'` | `1.29.2` 与 `20.10` 比错、两段版本号比错、`20.10.0-ce` 解析错 | 改 `sort -V` 比较；实测 12 组用例全对（含 `1.29.2 >= 20.10` = FAIL） |
| C14 | 阶段 3e `.env` 不存在时 `merge_env_config install.env install.env` 是空操作，随后 `rm install.env` | 首次部署**配置全丢**（静默回落到默认值） | 补"无 `.env` 则 `cp install.env .env`"分支 + 两者都没有时显式告警 |
| C15 | `set -u` 下跨阶段变量只在某分支内初始化 | `SKIP_CHECK=true` 时报 `CHECK_FAILED: unbound variable`，脚本直接死 | 所有跨阶段变量在分支**外**统一初始化 |
| C16 | 用 `sed -i` 改 `.env` | GNU sed 与 BSD sed 语义不同（后者要 `sed -i ''`），内网服务器版本杂 | 改「写临时文件再 `mv -f`」的可移植写法 |
| C17 | `command -v netstat` 当"工具可用"判据 | Git Bash / 精简镜像里的 netstat 是 stub，执行即非零；`set -e` 下 `inuse=$(...)` **静默终止整个脚本**（日志停在"端口占用检查（netstat）:"，无任何错误） | 探测工具时先真跑一次 `netstat -ltn`；循环内每处探测加 `\|\| true`（端口检查只是告警，不该有终止能力） |
| C18 | 没有 `code/` 目录时**整个阶段 3 被跳过** → 镜像不重建 | **源码目录直跑** or 纯镜像包两种形态下，新容器跑旧镜像里的代码，脚本一路打印"✅ 部署完成"（本次实测：改了一整轮代码，容器里 `dir()` 还是删除前的符号） | 增 `else` 分支：有 `backend/app` + `Dockerfile` 就按源码模式重建并置 `CODE_REBUILT`；构建失败直接 `exit 1`（宁可失败也不静默跑旧码） |
| C19 | `_read_peer_cpu_budget` 的 env 分支漏 `return` | env 命中后函数隐式返回 `None` → 调用方 `[0]` 抛 `TypeError: 'NoneType' object is not subscriptable` → **`/api/health` 直接 500**（比"投影不生效"严重得多） | 补 `return`；并加"所有取值都断言返回 tuple"的回归用例（类型标注不会替你兜底） |
| C20 | 测试未隔离投影的两条通道 | 部署跑过后容器内**真的有** `PEER_CELERY_CPU=3` 和 `data/.peer_cpu_budget` → 7 条无关用例集体变红（"无投影"前提不成立） | autouse fixture 同时屏蔽 env keys 与落盘候选路径；需要 env 通道的用例显式 `_enable_env_channel()` |

### 9.5 本次修复的"页面卡顿"缺陷（v4.1.6，2026-10-28）

> 用户症状："分析的时候刷新网页非常慢，而且提示此期间无法进行其他操作，只能一个用户用吗？"
>
> 三分之二的原因不在"分析"，而在**限流**——而且是三个缺陷叠加，
> 每一个单独看都"工作正常"（返回标准 429），合起来就是"全站一起卡"。

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| R1 | 限流 key 用 `request.client.host` | 容器部署下请求来自 nginx，在 Docker 网桥里**恒为 `172.18.0.x`** → **全站所有人共享一个 60 RPM 的桶**。一个用户的前端轮询就能把所有人拦死 | 改读 `X-Forwarded-For`（nginx 已透传）。**取最右一跳**而非最左：nginx 是追加语义，最左可被客户端伪造 → 取最左等于任何人伪造 IP 就能绕过限流 |
| R2 | 轮询与业务共用同一个桶 | 前端 4 处定时器（Layout 5s / Tasks 3s / 上传弹窗 1.5s / Dashboard 10s）≈ 78 次/分钟/标签页，光轮询就能打满 60 的桶 → **用户的真实操作拿到 429** | 拆两个桶：轮询桶 = 业务额度 × 4（240/分钟）；按**路径白名单 + GET + 无动作后缀**判定，`/tasks/{id}/cancel` 这类写操作仍走业务桶 |
| R3 | 前端无 429 退避重试 | `client.ts` 把 429 当普通失败抛出，`Layout` 里还是 `catch {}` 静默 → 表现为"数据不刷新/页面卡住"，用户只好反复刷新，越刷越糟 | GET 按 `Retry-After` 退避重试（最多 2 次、总等待 ≤3s）；写操作不重试（非幂等）；轮询组件遇 429 指数退避到 60s，成功后立即恢复 |

**验收（真机实测）：**

```
轮询 400 次并发          : 246 通过(401) / 154 被限流(429)
同时 30 次业务请求        : 30/30 通过  ← 分桶生效，业务没被饿死
用户 A 打满 200 次业务    : A 被限流 139 次
用户 B 同时 10 次业务     : B 10/10 通过  ← IP 隔离生效
回归                    : 568 passed / 1 skipped / 0 failed（新增 14 条限流用例）
```

**关于"只能一个用户用吗"的准确回答：**

- **不是**。生成任务的互斥（`task_guard`）只约束**同一个需求**不能同时跑两个生成，
  不同需求的多人可以同时跑；其他所有操作（浏览、查询、导出、编辑需求）从来都不受限。
- 之前"感觉只能一个人用"，是 R1+R2 让**限流**看起来像全局互斥——一个用户的轮询
  把桶抽干，别人的请求全 429。
- 界面上"（此期间无法进行其他分析操作）"这句提示仍保留，但它只描述**该需求**的生成互斥，
  不影响其他需求和其他操作。若觉得误导，可改成"该需求有任务在跑，暂不能新建分析"。
- 单进程内存桶的边界：多 worker / 多副本时每个进程各算一份额度，需换 Redis 版。
  本项目单 worker 单实例，不受影响。

**验收口径（开发机实测，i7-1165G7 4核8线程，api `cpus=6` / celery `cpus=3`）：**

```
api     : logical=6 physical=3 tpc=2 src=cpuinfo slots=2 encode_threads=3 ceiling=2
celery  : logical=3 physical=2 tpc=2 src=cpuinfo slots=1 encode_threads=2 dedup_workers=1
启动日志: [ResourceGuard] torch 线程已设定: intra-op=3 (物理核=3, 逻辑核=6, 来源=cpuinfo)
回归    : 554 passed / 1 skipped / 0 failed（unit 63 + 脚本式集成 74/91/190/112/141/7）
```

**跨容器投影的实测行为（同一台机器）：**

```
基线（无投影）      : 可用逻辑核 6.0 → 物理核 3 → ceiling 3
celery 占 2 核      : 可用逻辑核 4.0 → 物理核 2 → ceiling 2
celery 占 4 核      : 可用逻辑核 2.0 → 物理核 1 → ceiling 1（保底，不会降到 0）
撤销投影            : 回到 ceiling 3
```

**投影的三条通道与生效时机（v4.1.6 定案，现场排查必看）：**

| 通道 | 谁写 | 何时生效 | 用途 |
|---|---|---|---|
| 环境变量 `PEER_CELERY_CPU` | compose（值来自 `.env` / `deploy.sh` 导出） | 容器**重建**时 | 主通道。跨进程可见的唯一可靠方式 |
| 落盘 `data/.peer_cpu_budget` | `deploy.sh` / `set_peer_cpu_budget()` | 容器**重启**后（且需清 `_PEER_FILE_CACHE`） | 备通道。容器已建好、只想改投影时用 |
| ~~进程内 dict~~ | — | **永远不生效** | 已废弃。`docker exec` 是另一个进程（C7） |

> **改投影的正确姿势**：改 `.env` 的 `PEER_CELERY_CPU` → `up -d --force-recreate backend`。
> 只 `restart` **不够**（容器环境变量在创建时固定，restart 不刷新）。
> 只改落盘文件也不够——`_PEER_FILE_CACHE` 一个进程只读一次（防探活做同步 IO）。

**deploy.sh 里投影的执行顺序是硬约束（C9）：**

```
阶段 5-3  算 celery cpus → 导出 PEER_CELERY_CPU → 写 data/.peer_cpu_budget → 写 .env
阶段 5    up -d（env 变了 → compose 自动 Recreate；本次改过镜像则 --force-recreate）
阶段 6-2a 从 api 主进程读 health_brief，**验证** cpu_peer_budget 真的非空
```

> 第 3 步是重点：脚本自报"注入成功"曾经是假信号（写进了另一个进程）。
> 现在只有"从主进程读回来"才算通过；读不到会打印橙色告警和手工修复命令。

---

## 十、快速索引

**我该看哪一节？**

| 你的情况 | 看这里 |
|---|---|
| 第一次在内网部署 | 第〇节自检 → 第二节宿主配置 → 第四节流程 → 第五节验证 |
| 已装过，要升级版本 | 第四节 4.1「升级迁移」+ 4.2 版本保护 → 第五节验证 |
| 部署后 `mem_source: proc` | 5.3 排查顺序（**注意第 3/4 步顺序**） |
| 3.10 上 Milvus 起不来 | 第二节第 1 项（`vm.max_map_count`）、第 2 项（memlock） |
| 3.10 上容器起了但外网连不上 | 第二节第 3 项（iptables 后端） |
| 想确认这台机器跑的是什么版本 | `cat DEPLOY-RECORD.md` → 第二节迁移记录表 |
| 想回到"不限制内存" | 第七节回滚 |
| 换个内核版本（3.10 ↔ 4.x） | 第一节表格 + 第二节「4.x 需要做什么」 |

**三条最容易忘的事：**

1. **手工 compose 命令必须带 `-f docker-compose.resources.override.yml`**，否则配额丢失
2. **迁移完必须往第二节的迁移记录表追加一行**，否则下次迁移要重新验一遍
3. **`docker compose up -d` 不会因配额变更重建容器**，只改配额时要加 `--force-recreate`

---

> 文档维护：本文件是内网迁移的唯一入口。修改部署脚本或宿主配置要求时，
> 请同步更新对应章节，并在第九节 9.3 记录新的缺陷编号。


# Memory 原件/派生件两阶段提交与崩溃恢复 - 产品需求文档

## Overview
- **Summary**: 为每条 Snapchat Memory 建立「raw 原件」与「派生件」两个明确状态。媒体字节先落同目录临时原件，计算摘要后原子提交为只读、内容寻址的 raw 对象；EXIF 丰富在独立临时派生件上进行，重新解析校验通过后才原子发布到时间戳命名的用户可见路径。维护源记录指纹、raw 摘要、派生路径与 enrichment 状态的最小索引，并支持崩溃恢复与升级前平铺文件的显式迁移。
- **Purpose**: 现状代码将 CDN 字节直接写入最终路径、EXIF 在同一路径原地改写、失败被静默吞掉（`except: pass`），导致：磁盘写满/进程崩溃会留下半写文件；EXIF 失败无法与下载成功区分、无法在不重新请求 CDN 的情况下重试；升级前已有平铺文件与已验证原件无法区分。
- **Target Users**: 使用本脚本下载 Snapchat Memories 的终端用户（关心文件完整性与可恢复性），以及需要可重复验证行为的维护者/测试。

## Goals
- raw 原件字节与 CDN 响应逐字节一致，内容寻址、只读、不可被 enrichment 修改。
- 用户可见派生件只有在「完整写入 + 重新解析校验通过」后才原子发布。
- 最小索引记录：源记录指纹、raw 摘要、raw/派生路径、raw 来源、enrichment 状态，作为断点续跑的唯一事实来源。
- EXIF 解析/写入失败可从 raw 离线重试，不再请求 CDN；统计中不得显示为完整成功。
- raw 提交、派生写入、索引更新三个阶段崩溃或 ENOSPC 后，重启只能观察到「上一完整状态」或「可识别待清理临时项」。
- 升级前平铺 jpg/mp4 默认不纳入索引、不视为已验证 raw；仅在显式迁移时登记为来源未知。

## Non-Goals
- 不改变 memories_history.json 的输入格式与用户可见文件的时间戳命名规则（`YYYY-MM-DD_HH-MM-SS.ext`）。
- 不处理同一时间戳多条 Memory 的文件名冲突（现状即存在，超出本次范围）。
- 不对视频（mp4 等）做任何元数据注入。
- 不做远程/云存储后端、不做多进程/多机器共享索引。
- 不重写并发模型（仍为 asyncio 单进程事件循环）。

## Background & Context
- 现状见 `main.py`：`add_exif_data` 在最终路径原地读写（L64-94）；`download_memory` 直接 `write_bytes` 到最终路径（L110）；失败静默（L93-94）；`skip_existing` 仅凭平铺文件是否存在（L137-143）。
- 已验证：`exif>=1.6.1` 可对不含 EXIF 的最小 JPEG 写入 DateTimeOriginal 与 GPS DMS，输出仍为合法 JPEG（FFD8 起始），重新解析可读回全部标签。
- 运行环境：uv 管理的 CPython 3.13；依赖 exif/httpx/pydantic/tqdm，不新增运行时依赖；测试仅新增 pytest 作为开发依赖。

### 存储布局（用户已确认：位于输出目录内）
```
<output>/
  2020-01-02_03-04-05.jpg            # 用户可见派生件（时间戳命名）
  2020-01-03_10-00-00.mp4
  .memories/
    index.json                       # 最小索引（原子写）
    .index.json.tmp-<uuid>           # 索引临时项（可识别、可清理）
    raw/
      ab/<sha256>.jpg                # 只读 raw 对象，按摘要分片内容寻址
      .rawtmp-<uuid>                 # raw 临时原件（与 raw 同目录，可识别）
  .2020-01-02_03-04-05.jpg.tmp-<uuid> # 派生临时件（与用户可见路径同目录，可识别）
```

### 索引条目（最小字段）
```json
{
  "version": 1,
  "entries": {
    "<源记录指纹>": {
      "date": "2020-01-02 03:04:05",
      "ext": ".jpg",
      "raw_digest": "<sha256 hex>",
      "raw_path": ".memories/raw/ab/<sha256>.jpg",
      "raw_source": "cdn | unknown",
      "derived_path": "2020-01-02_03-04-05.jpg",
      "enrichment": "pending | success | not_required | failed | unknown",
      "updated_at": "<iso8601>"
    }
  }
}
```
- 源记录指纹：对源 JSON 记录的 `Date`、`Download Link`、`Location` 原始值做规范化 JSON 后取 sha256；同一导出文件重跑稳定。
- `raw_source=cdn`：经流水线下载、摘要与实际观测的 CDN 字节一致；`unknown`：显式迁移的升级前文件。
- `enrichment`：`pending`（raw 已提交、派生未完成）、`success`（JPEG EXIF 注入并校验通过）、`not_required`（--no-exif 或非 JPEG，raw 原样发布）、`failed`（尝试过且失败，raw 仍在）、`unknown`（迁移登记，从未经流水线处理）。

### 两阶段提交协议
- **阶段 A raw**：POST 取 CDN URL → GET 取字节 → sha256 → 写 `.memories/raw/.rawtmp-<uuid>` 并 fsync → `os.replace` 到内容寻址路径 → fsync 目录 → `chmod 0o444` → 原子更新索引（raw_source=cdn，enrichment=pending）。同摘要对象已存在则直接复用。
- **阶段 B 派生**：
  - not_required：复制 raw 字节到输出根目录临时派生件 → fsync → 设置 mtime → `os.replace` 发布 → fsync 目录 → 索引记 not_required。
  - success：复制 raw 字节到独立临时派生件（绝不打开 raw 写入）→ 在临时件上注入 EXIF → **重新解析临时件并断言 DateTimeOriginal 与（有有效 GPS 时的）GPS 坐标标签存在且正确** → fsync → 设置 mtime → `os.replace` 发布 → fsync 目录 → 索引记 success + derived_path。
  - 任何异常：删除临时派生件，索引记 failed（可含错误类别），不发布。
- 所有发布只经同目录 `os.replace`（POSIX 原子），索引只经「临时文件 + fsync + replace」更新，先 fsync 文件再 fsync 目录。

### 续跑决策（索引驱动）
- 有条目且 raw 对象可读：success/not_required 且派生件存在 → 跳过（--no-skip-existing 时强制重下）；failed/pending 或缺派生件 → 仅从 raw 重跑阶段 B，**零 CDN 请求**。
- 有条目但 raw 对象缺失：清除该条 raw 引用，按无 raw 处理（允许重新下载）。
- 无条目：走完整网络流水线。
- 无条目但目标路径存在升级前平铺文件：默认跳过并告警，不覆盖（用户已确认）；--no-skip-existing 可强制走新流水线。

## Functional Requirements
- **FR-1**：媒体响应字节先写入 raw 同目录临时文件，计算摘要、fsync 后原子提交为内容寻址只读 raw 对象；摘要对字节计算。
- **FR-2**：启用 EXIF 的 JPEG，派生件在独立临时文件上注入 EXIF，重新解析校验 DateTimeOriginal 及有效 GPS 坐标后，原子发布到时间戳命名路径；派生件与 raw 物理分离、非同路径覆盖。
- **FR-3**：--no-exif 或非 JPEG 时，将未修改的 raw 内容直接发布为派生件（字节与 raw 一致），状态记 not_required。
- **FR-4**：维护最小索引（指纹、raw 摘要、raw_path、derived_path、raw_source、enrichment），所有更新原子化；索引为续跑/跳过/重试的唯一事实来源。
- **FR-5**：EXIF 解析或写入失败被注入时：raw 仍可按摘要读取；enrichment 明确为 failed；Downloaded 不计完整成功（单列 enrichment 失败数）；再次运行不请求 CDN 即从 raw 重试。
- **FR-6**：raw 提交、派生写入/发布、索引更新各阶段可注入 ENOSPC 或进程崩溃；重启恢复删除可识别临时项、对账索引与已发布文件，之后重跑收敛到完整成功状态。
- **FR-7**：启动恢复扫描仅清理固定命名模式的临时项（raw/派生/索引三类），绝不触碰用户文件与已发布派生件。
- **FR-8**：默认扫描发现升级前平铺 jpg/mp4 时：不建索引条目、不标已验证 raw、不覆盖，计入 legacy 并告警提示迁移。
- **FR-9**：显式 `--migrate-legacy`：文件保留原路径；登记 raw_source=unknown、enrichment=unknown，按文件实际字节记录事实摘要但不伪造 CDN 来源；能匹配 JSON 记录的用源记录指纹，孤立文件用合成身份；迁移后正常运行视为已跟踪。
- **FR-10**：提供不影响生产路径的故障注入点（环境变量启用），覆盖 raw 提交、派生写入、索引更新与 EXIF 失败，供测试模拟 ENOSPC/崩溃。

## Non-Functional Requirements
- **NFR-1（持久性）**：崩溃恢复后不得存在半写的用户可见派生件；任何对外可见路径上的文件内容要么是上一完整版本，要么是新完整版本。
- **NFR-2（可测试性）**：网络层可注入 httpx transport；故障点集中管理、默认完全 no-op；pytest 测试覆盖正常/失败/恢复/迁移矩阵，含跨进程（子进程 os._exit）崩溃用例。
- **NFR-3（兼容性）**：Python 3.13；不新增运行时依赖；保留现有 CLI 参数语义并新增迁移开关；并发下载仍可工作且索引更新在并发下安全。
- **NFR-4（可维护性）**：职责按模块拆分（模型/索引/存储提交/EXIF/流水线/迁移/故障点），main.py 保持薄入口。

## Constraints
- **Technical**: 依赖 POSIX 同目录 rename 原子性与 fsync 语义（开发与运行环境为 macOS/本地文件系统）；只读权限用 chmod 0o444（不防恶意删除，仅防意外改写）。
- **Business**: 用户可见文件名与目录结构（除新增隐藏 `.memories/` 外）保持不变。
- **Dependencies**: exif/httpx/pydantic/tqdm 已在 requirements.txt；pytest 仅作开发依赖（uv 运行）。

## Assumptions
- 单进程内 asyncio 并发；索引更新用进程内锁串行化，原子替换保证跨进程崩溃安全。
- 内容寻址路径以 sha256 命名，天然去重；同摘要即同内容，无需逐字节复核。
- Snapchat CDN 对同一下载链接的响应字节在单次导出有效期内可信；raw 摘要只承诺与「实际观测到的响应字节」一致。
- 时间戳命名冲突维持现状行为（后发布者原子替换），不在本次范围。

## Acceptance Criteria

### AC-1: raw 原件内容寻址与只读提交
- **Type**: `rule`
- **Given**: 一条 Memory 经流水线下载成功
- **When**: 检查 `.memories/raw/` 下对象
- **Then**: raw 位于以 sha256 摘要命名的分片路径；权限为 0o444；`sha256(raw 字节) == sha256(测试 CDN 响应字节)`；raw 目录中无残留 `.rawtmp-*`。
- **Pass Condition**: 上述四项全部成立（测试断言通过）。
- **Evidence**: pytest 正常路径用例对文件内容 hexdigest、权限位与目录列表的断言输出。

### AC-2: JPEG 派生件校验发布、标签完整且与 raw 分离
- **Type**: `rule`
- **Given**: 启用 EXIF、带有效 GPS 坐标的 JPEG Memory
- **When**: 流水线完成阶段 B
- **Then**: 用户可见路径为 `<date>.jpg`；重新解析得到 DateTimeOriginal 等于 Memory 日期，GPS 纬度/经度及 N/S、E/W 引用存在且数值正确；raw_path 与 derived_path 不同路径、不同 inode；raw 字节仍与 CDN 字节逐字节一致（未被原地改写）；索引 enrichment=success。
- **Pass Condition**: 全部断言通过。
- **Evidence**: pytest 用例重新解析派生件、比较路径/inode/raw 摘要与索引状态。

### AC-3: --no-exif 与非 JPEG 原样发布
- **Type**: `rule`
- **Given**: 分别以 --no-exif 下载 JPEG、默认下载 mp4
- **When**: 阶段 B 完成
- **Then**: 派生件字节摘要等于 raw 摘要；状态 not_required；raw 与派生件路径不同；mtime 为 Memory 时间。
- **Pass Condition**: 两类输入的断言均通过。
- **Evidence**: pytest 对应用例。

### AC-4: 最小索引原子持久化并驱动续跑
- **Type**: `rule`
- **Given**: 一次完整运行
- **When**: 读取 `.memories/index.json`
- **Then**: 每条形如指纹→{raw_digest, raw_path, derived_path, raw_source, enrichment, date, ext}；无多余字段；同参数重跑全部跳过且无 CDN 请求。
- **Pass Condition**: schema 校验与重跑零请求断言通过。
- **Evidence**: pytest schema 断言 + CDN 请求计数。

### AC-5: EXIF 失败隔离与免 CDN 重试
- **Type**: `rule`
- **Given**: 注入 EXIF 解析失败一次（首次运行）
- **When**: 首次运行后检查并再次运行
- **Then**: raw 对象存在且可按摘要读取、字节不变；目标派生路径不存在；索引 enrichment=failed；汇总输出 Downloaded 不含该条且单列 enrichment 失败；第二次运行 CDN GET 次数为 0、从 raw 重试并成功发布、索引转 success。
- **Pass Condition**: 首轮与次轮全部断言通过。
- **Evidence**: pytest（含子进程故障注入）用例。

### AC-6: raw 提交阶段故障恢复
- **Type**: `rule`
- **Given**: 在 raw 原子提交点分别注入 ENOSPC（进程内）与进程崩溃（子进程 os._exit）
- **When**: 重启执行启动恢复后重跑
- **Then**: 不出现引用缺失 raw 的索引条目；`.rawtmp-*` 被清理为 0；重跑后 raw/派生件/索引三者一致且 enrichment=success；内容满足 AC-1/AC-2。
- **Pass Condition**: 两种注入模式断言均通过。
- **Evidence**: pytest 进程内 ENOSPC 用例 + 子进程崩溃用例。

### AC-7: 派生写入阶段故障不产生半写发布件
- **Type**: `rule`
- **Given**: 在派生临时件写入/发布点注入 ENOSPC 或进程崩溃
- **When**: 重启恢复并检查输出根目录后重跑
- **Then**: 用户可见路径上从未出现半写文件（崩溃场景：文件不存在；ENOSPC 场景：文件不存在）；仅存在可识别 `.*.tmp-*` 临时项且恢复后被清理；raw 完整；重跑零 CDN GET 即收敛成功。
- **Pass Condition**: 故障后、恢复后、重跑后三时点断言全部通过。
- **Evidence**: pytest 对应用例（含发布前后存在性检查）。

### AC-8: 索引更新阶段故障的状态对账
- **Type**: `rule`
- **Given**: 在阶段 A、阶段 B 后的索引更新点注入崩溃/ENOSPC
- **When**: 重启恢复
- **Then**: index.json 始终是合法完整 JSON（上一版或新版，无半截 JSON）；若派生件已发布但索引仍旧，恢复经重新校验后对账为 success/not_required；若派生件无效或缺失则回到 pending 从 raw 重建；重跑零 CDN GET 收敛。
- **Pass Condition**: 两个索引更新点的注入均满足上述断言。
- **Evidence**: pytest 子进程 + 进程内用例。

### AC-9: 升级前平铺文件默认不被信任
- **Type**: `rule`
- **Given**: 输出目录已存在平铺 `<date>.jpg`/`.mp4` 且索引为空
- **When**: 默认运行流水线
- **Then**: 不创建索引条目、不在 raw 区登记对象、不覆盖/修改原文件；这些 Memory 计入 legacy 跳过并出现告警；网络计数为 0。
- **Pass Condition**: 断言全部通过。
- **Evidence**: pytest legacy 用例。

### AC-10: 显式迁移保留路径并记录来源未知
- **Type**: `rule`
- **Given**: AC-9 的平铺文件（含一个 JSON 中无记录的孤立文件）
- **When**: 执行 `--migrate-legacy` 后再正常运行
- **Then**: 文件保留原路径（inode 不变）；索引有条目，raw_source=unknown、enrichment=unknown、raw_path 为空、raw_digest 为文件实际字节摘要、derived_path 指向原路径；孤立文件持合成指纹且同样 raw_source=unknown；迁移后正常运行视为已跟踪而跳过；全程不产生 CDN 请求。
- **Pass Condition**: 全部断言通过。
- **Evidence**: pytest 迁移用例。

### AC-11: 启动恢复仅清理可识别临时项
- **Type**: `rule`
- **Given**: 在 raw 区、输出根、.memories 区预置三类临时文件及一个名称不含临时模式的用户文件
- **When**: 执行启动恢复
- **Then**: 三类临时项被删除；用户文件与已发布派生件原样保留。
- **Pass Condition**: 删除/保留两组断言通过。
- **Evidence**: pytest 恢复用例。

### AC-12: 实现结构与故障注入卫生
- **Type**: `rubric`
- **Dimension**: 模块清晰度、可测试性与故障点卫生
- **Scale**: 1-5
- **Anchors**: 1 = 逻辑仍耦合在单文件、故障注入污染正常路径；3 = 有模块拆分但提交/恢复职责交叉、部分故障点需改测试才能模拟；5 = 模型/索引/存储/EXIF/流水线/迁移/故障点职责单一，生产路径无注入代码分支行为（仅集中 no-op 探针），测试矩阵完整可读。
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查 + 测试目录结构与用例命名。

## Open Questions
- 无（三个布局/冲突/孤立文件问题已由用户确认；时间戳冲突维持现状列为 Non-Goal）。

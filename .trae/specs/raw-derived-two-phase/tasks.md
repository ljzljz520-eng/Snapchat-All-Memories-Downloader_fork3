# Memory 原件/派生件两阶段提交 - 实现计划

说明：每个任务包含自身的测试需求（TR），规则类 TR 需可客观判定。故障点命名集中在 `faults.py`，通过环境变量 `MEMORIES_FAULT=<point>:<exit|enospc|error>` 启用，默认 no-op。

## Task 1: 工程骨架与测试基建
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新建包 `memories_dl/`（`__init__.py`、`models.py`、`index.py`、`store.py`、`enrich.py`、`pipeline.py`、`recovery.py`、`legacy.py`、`faults.py`、`cli.py`、`layout.py`），`main.py` 改为薄入口调用 `cli.main`。
  - `faults.py`：`inject(point)` 探针；仅当环境变量匹配时触发 `os._exit(99)`、`OSError(ENOSPC)` 或普通异常；无环境变量时零行为。
  - pytest 以 dev dependency-group 加入（pyproject.toml）；`tests/conftest.py`：MockTransport 假 CDN（POST/GET 分离计数）、1x1 合法 JPEG 与 mp4 夹具、临时目录 fixture、后台线程真实 loopback HTTP server、子进程 `run_cli` 与 JSON 生成器。
- **Acceptance Criteria Addressed**: AC-12（部分）
- **Test Requirements**:
  - `rule` TR-1.1: `uv run python -m pytest -q` 可收集且全部通过。
  - `rule` TR-1.2: 不设环境变量时 `inject` 零行为；`p:enospc` 仅在点 p 抛 ENOSPC。
  - `rule` TR-1.3: MockTransport 分别处理 POST/GET 且计数器独立。
- **Completion Evidence**:
  - TR-1.1：全套件 49 passed（3.59s）。
  - TR-1.2/1.3：test_faults.py 4 例、流水线/子进程用例对 posts/gets 计数断言全部 PASSED。

## Task 2: 模型、源记录指纹与结果/统计类型
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `Memory` 迁移至 `models.py`（日期解析、Location 坐标解析、filename、source_record、fingerprint）。
  - `source_fingerprint` 对 Date/Download Link/Location 规范化 JSON（sort_keys、紧凑分隔符）取 sha256。
  - `IndexEntry`、`RawSource(cdn|unknown)`、`Enrichment(pending|success|not_required|failed|unknown)`、`Outcome`、`Stats`（新增 enrich_failed、legacy）。
- **Acceptance Criteria Addressed**: AC-4（字段部分）
- **Test Requirements**:
  - `rule` TR-2.1: 同记录（乱序）指纹相同；任一字段改变指纹不同。
  - `rule` TR-2.2: 有/无 Location 的解析行为保持。
  - `rule` TR-2.3: 枚举非法值校验拒绝。
- **Completion Evidence**:
  - test_models.py 5 例 PASSED（指纹稳定性/敏感性、坐标、枚举校验）。

## Task 3: 最小索引的原子存储
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - `index.py` `IndexStore`：缺失→空索引；损坏主文件抛 `IndexCorruptError`；upsert/replace_many 经 `.index.json.tmp-<uuid>`+fsync+`os.replace`+fsync 目录提交，内存状态在持久化后推进；asyncio.Lock；version=1；行内不重复 fingerprint。
- **Acceptance Criteria Addressed**: AC-4, AC-8
- **Test Requirements**:
  - `rule` TR-3.1: 重载字段精确（8 个行字段）、并发 25 upsert 不丢。
  - `rule` TR-3.2: 每次 upsert 后主文件可解析；临时名固定前缀。
  - `rule` TR-3.3: 缺失空加载；损坏显式报错。
- **Completion Evidence**:
  - test_index.py 6 例 PASSED（schema 键集合断言、并发、中途解析、损坏、临时名 spy）。

## Task 4: 阶段 A —— raw 临时原件到只读原子提交
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - `store.py`：sha256 先行 → 同分片目录 `.rawtmp-<uuid>` 写入 fsync → `raw_write`/`raw_commit` 探针 → `os.replace` → chmod 0o444 → fsync 目录；同摘要去重复用；异常清理临时项；`read_raw` 只读。
- **Acceptance Criteria Addressed**: AC-1, AC-6
- **Test Requirements**:
  - `rule` TR-4.1: 摘要与输入一致、0o444、无临时残留、路径分片正确。
  - `rule` TR-4.2: 同字节复用同 inode。
  - `rule` TR-4.3: raw_commit ENOSPC 无对象、无索引、无残留；raw 拒绝写打开。
- **Completion Evidence**:
  - test_store.py 4 例 PASSED；AC-1 的字节一致性另由 test_pipeline.py::test_happy_jpeg_full_contract 端到端断言（raw == JPEG_BYTES）。

## Task 5: 阶段 B —— EXIF 临时派生件校验发布与原样发布
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - `enrich.py`：raw 全程只读；passthrough 复制字节→临时件→原子发布；EXIF 路径在独立临时件注入标签，`get_file()` 后重新解析断言 DateTimeOriginal/Digitized、GPS DMS 与 ref（无 GPS 时断言无标签），再 fsync/utime/`os.replace`；故障点 exif_parse/exif_write/derived_write/derived_commit；失败清理临时件并抛分类 `EnrichError`。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-5, AC-7
- **Test Requirements**:
  - `rule` TR-5.1: 标签读回正确、raw 未变、路径/inode 双异、mtime。
  - `rule` TR-5.2: passthrough（jpg/mp4）摘要等于 raw。
  - `rule` TR-5.3: 无 GPS 不含 GPS 标签但含 DTO。
  - `rule` TR-5.4: exif_parse 注入 → 无目标、无临时、raw 不变、EnrichError(parse)。
  - `rule` TR-5.5: derived_commit ENOSPC 不发布、临时清理。
- **Completion Evidence**:
  - test_enrich.py 5 例 PASSED。

## Task 6: 流水线编排、续跑决策与免 CDN 重试
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 5
- **Description**:
  - `pipeline.py`：transport 可注入；POST→GET→commit_raw→index(pending)→阶段 B→index(success/not_required|failed)；本地有 raw 时阶段 B 零网络；unknown 迁移行视为已跟踪；legacy 占用默认 LEGACY_SKIPPED 告警；阻断 IO 走 to_thread；汇总行新增 Enrichment failed/Legacy，仅 SUCCESS 计 Downloaded。
- **Acceptance Criteria Addressed**: AC-4, AC-5
- **Test Requirements**:
  - `rule` TR-6.1: 重跑全跳过且 posts/gets 不增。
  - `rule` TR-6.2: 首轮 enrich 失败 Downloaded=0/failed=1/raw 在；次轮 GET 零增长→success。
  - `rule` TR-6.3: 派生被删零 GET 重建。
  - `rule` TR-6.4: raw 缺失允许重下并收敛。
  - `rule` TR-6.5: 500 计入 Failed 且不产生任何工件。
- **Completion Evidence**:
  - test_pipeline.py 7 例 PASSED，其中免 CDN 重试以同一 FakeCDN 跨两轮断言 gets==1。

## Task 7: 启动恢复与三阶段崩溃对账
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 6
- **Description**:
  - `recovery.py`：先扫三类固定模式临时项（raw rglob / index / 输出根派生临时），名称不匹配一律不动；再对账：cdn 行 raw 缺失→清 raw 声明回 pending；pending/failed 但派生存在→JPEG 重解析（有 Memory 时严格对标签）、非 JPEG 对摘要，通过则升级 success/not_required，否则删除回 pending；success/not_required 派生缺失→回 pending；unknown 行不裁决。
  - 四个子进程退出点：raw_commit、derived_write、index_update_raw、index_update_derived（os._exit(99)）。
- **Acceptance Criteria Addressed**: AC-6, AC-7, AC-8, AC-11
- **Test Requirements**:
  - `rule` TR-7.1: 三类临时删除、用户文件/已发布件保留。
  - `rule` TR-7.2: raw_commit 子进程崩溃后恢复+重跑收敛、工件一致。
  - `rule` TR-7.3: derived_write（子进程 exit + 进程内 ENOSPC）后目标从无半写、重跑 GET 零增长。
  - `rule` TR-7.4: 两个索引更新点崩溃后 JSON 恒完整；已发布派生对账升级无需网络。
  - `rule` TR-7.5: 非临时命名文件负样本不动。
- **Completion Evidence**:
  - test_recovery.py 6 例 + test_crash_subprocess.py 4 例全部 PASSED；子进程用真实 loopback server 计数断言跨进程零 CDN 重试。

## Task 8: 升级前平铺文件的默认扫描与显式迁移
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 7
- **Description**:
  - `legacy.py`：时间戳命名 jpg/jpeg/mp4 且不在索引 derived_path 集合内即为 legacy；默认运行跳过+告警；`migrate_legacy` 原位登记 raw_digest=实际摘要、raw_path=None、raw_source=unknown、enrichment=unknown；匹配 JSON 用源指纹，孤立文件用 `legacy:<sha1(name)>`。
- **Acceptance Criteria Addressed**: AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-8.1: 默认运行无条目/无 raw 对象、inode 字节不变、legacy=2、零网络、告警含迁移提示。
  - `rule` TR-8.2: 迁移原 inode、字段全 unknown/空 raw_path/事实摘要、孤立合成指纹、零网络。
  - `rule` TR-8.3: 迁移后正常运行 skipped 跟踪。
  - `rule` TR-8.4: --no-skip-existing 强制走流水线并原子替换为 cdn/success。
- **Completion Evidence**:
  - test_legacy.py 5 例 PASSED；CLI 迁移另由 test_cli.py::test_migrate_cli_registers_and_does_not_download 子进程验证。

## Task 9: CLI 接线、并发回归与整体验收
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - `cli.py` 保留全部旧参数并新增 `--migrate-legacy`；启动先 `run_recovery` 再下载/迁移；main.py 薄入口；README 同步用法与存储模型说明；混合并发场景回归。
- **Acceptance Criteria Addressed**: AC-12
- **Test Requirements**:
  - `rule` TR-9.1: --help 含全部参数；迁移模式退出码 0 且零下载。
  - `rule` TR-9.2: 并发=8 混合 8 条（GPS JPEG/无 GPS JPEG/mp4）自洽且无临时残留。
  - `rule` TR-9.3: 全套件通过。
  - `rubric` TR-9.4: 模块清晰度/可测试性/故障点卫生；scale 1-5；anchors 见 AC-12；threshold >= 4。
- **Completion Evidence**:
  - TR-9.1/9.2：test_cli.py 3 例 PASSED。
  - TR-9.3：`uv run python -m pytest tests/ -q` → 49 passed in 3.59s。
  - TR-9.4 自评分 5：职责按 11 个模块单一拆分（models/layout/faults/index/store/enrich/recovery/legacy/pipeline/cli + 薄 main）；生产路径无注入分支（注入仅集中在 faults.inject 探针，默认零行为）；测试按任务矩阵分 9 个文件、49 例，含真实跨进程 os._exit 崩溃与 ENOSPC 注入。最终分数以独立 Review 为准。

---

## Review R1 修复项

## Issue I-1: 派生临时项识别收紧为完整固定模式（F-1, actionable）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1
- **Description**:
  - `layout.is_derived_tmp` 当前为 `startswith(".") and ".tmp-" in name`，会把用户自有、仅含 `.tmp-` 子串的点文件（如 `.vacation.photo.tmp-backup.jpg`）当作临时项在每次启动恢复时删除。raw/index 两个谓词同样缺少 uuid 后缀形态约束。
  - 改为与生成端严格互逆的完整形态匹配：派生 `^\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.(jpg|jpeg|mp4)\.tmp-[0-9a-f]{32}$`；raw `^\.rawtmp-[0-9a-f]{32}$`；index `^\.index\.json\.tmp-[0-9a-f]{32}$`。
- **Acceptance Criteria Addressed**: AC-11, AC-7
- **Test Requirements**:
  - `rule` TR-I-1.1: 输出根预置 `.vacation.photo.tmp-backup.jpg`、`.rawtmp-short`、`.index.json.tmp-xyz` 等非本工具形态文件，run_recovery 后全部保留；合法三类临时项仍被删除。
  - `rule` TR-I-1.2: 全套件回归通过。
- **Completion Evidence**:
  - layout.py 三个谓词改为与生成端互逆的完整正则（32 位 hex uuid；派生件锚定时间戳形态与 jpg/jpeg/mp4 扩展名）。
  - TR-I-1.1：test_recovery.py::test_cleanup_only_removes_recognizable_temps 增加 `.vacation.photo.tmp-backup.jpg`、`.my.tmp-999`、`.rawtmp-short`、`.index.json.tmp-xyz` 四个负样本，断言全部保留、合法三类临时项仍删除，PASSED。
  - TR-I-1.2：52 passed。

## Issue I-2: EXIF 校验重读磁盘临时件并保证写满（F-2, advisory→采纳）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1
- **Description**:
  - `publish_exif` 校验的是内存 `enriched` 缓冲而非磁盘临时件，与 FR-2「重新解析临时件」的字面协议不一致；三处 `os.write` 均为单次写，无短写保护。
  - 统一 `store.write_all(fd, data)` 循环写满（store/index/enrich 共用）；EXIF 发布时写入 tmp 后从 tmp 路径重新读字节做 `_verify`。
- **Acceptance Criteria Addressed**: AC-2, AC-7
- **Test Requirements**:
  - `rule` TR-I-2.1: monkeypatch 使首次 os.write 短写，write_all 重试后落盘字节完整，发布成功。
  - `rule` TR-I-2.2: test_enrich/test_crash_subprocess 回归通过，标签断言不弱化。
- **Completion Evidence**:
  - store.write_all 循环写满，接入 store.commit_raw / index._commit / enrich._write_tmp；publish_exif 写入 tmp 后 read_bytes 回读并断言与缓冲一致，再对回读字节执行 _verify。
  - TR-I-2.1：test_store.py::test_write_all_retries_short_writes（首次 os.write 仅报 40 字节）落盘 1000 字节完整，PASSED。
  - TR-I-2.2：test_enrich 5 例与 test_crash_subprocess 4 例回归 PASSED。

## Issue I-3: raw 去重复用分支归一化只读权限（F-3）
- **Status**: `completed`
- **Priority**: low
- **Depends On**: None
- **Discovered By**: Review R1
- **Description**:
  - commit_raw 在对象已存在时直接返回，不补 chmod；若 replace→chmod 窗口中断过，对象会永久停留在 0o644。
  - 复用分支对最终对象补 chmod 0o444（并吞掉 ENOENT 竞态）。
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `rule` TR-I-3.1: 预置同摘要 0o644 对象，commit_raw 后权限归一为 0o444。
- **Completion Evidence**:
  - commit_raw 复用分支补 chmod 0o444（吞 FileNotFoundError 竞态）。
  - TR-I-3.1：test_store.py::test_reuse_branch_normalizes_permissions（预置 0o644 同摘要对象→提交后 0o444）PASSED。

## Issue I-4: UNKNOWN 迁移行派生件缺失时默认不得静默改写溯源（F-4）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1
- **Description**:
  - UNKNOWN 行仅在派生存在时 skip；派生缺失时默认运行落入网络段，重下并把该行改写成 raw_source=cdn，违背「迁移后视为已跟踪」。
  - 默认运行（skip_existing）对「UNKNOWN 行 + 派生缺失」按 LEGACY_SKIPPED 处理并告警（提示重新迁移或 --no-skip-existing），零网络、不改写溯源；--no-skip-existing 仍可强制重下。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rule` TR-I-4.1: 迁移后删除派生件，默认运行 legacy 跳过、GET=0、行仍为 unknown；--no-skip-existing 才重下并转 cdn/success。
- **Completion Evidence**:
  - pipeline.py：skip_existing 下 UNKNOWN 行派生缺失→LEGACY_SKIPPED 告警（提示重新迁移或 --no-skip-existing），零网络且不改写行；skip_existing=False 才走网络重下并转 cdn/success。
  - TR-I-4.1：test_legacy.py::test_migrated_row_with_missing_file_stays_untracked_source 两轮断言（默认 legacy=1/GET=0/溯源仍 unknown；强制后 downloaded=1/GET=1/cdn/success）PASSED。


## Issue I-5: 恢复对账覆盖真实崩溃窗口，迁移不得覆盖在途 cdn 行（F10, actionable）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R2
- **Description**:
  - 崩溃点 index_update_derived 后真实形态为「派生件已耐久发布、索引行仍 cdn/pending 且 derived_path=None」。旧恢复只经 entry.derived_path 找派生件，对该形态不做 AC-8 要求的重新校验对账。
  - 该状态下运行 --migrate-legacy，已发布文件会被当作升级前平铺文件登记，replace_many 把 cdn/pending 行整体覆盖为 unknown，raw_digest 改写为派生字节摘要，raw 对象成为孤儿，此后默认运行永久 Skipped。
  - 修复：IndexEntry 增加 expected_derived_rel/expected_derived_file（行 date/ext 推导确定性目标路径）；恢复对 cdn 行在 derived_path 为空时按确定性路径找回已发布件，读盘重新分类（摘要==raw_digest→not_required；JPEG 严格 EXIF→success；无效→删除回 pending），成功时一并补写 derived_path。
  - 防御：migrate_legacy 对 raw_source=cdn 的指纹行、以及在途 cdn 行（derived_path 为空）的确定性目标路径一律跳过并告警，绝不写 unknown。
- **Acceptance Criteria Addressed**: AC-8、AC-9、AC-10
- **Test Requirements**:
  - `rule` TR-I-5.1: index_update_derived 故障后仅执行 run_recovery（无 pipeline），jpg+EXIF / jpg+--no-exif / mp4 分别对账为 success/not_required/not_required，derived_path 补齐，零 CDN。
  - `rule` TR-I-5.2: 崩溃后直接调用 migrate_legacy（不经恢复）：返回空、stdout 告警跳过、行保持 cdn/pending、raw 不孤儿。
  - `rule` TR-I-5.3: 子进程 exit 崩溃后 CLI --migrate-legacy：行保持 cdn（启动恢复对账为 success）、raw_digest 等于 CDN 字节摘要、输出 No untracked legacy files found。
  - `rule` TR-I-5.4: 子进程 exit 崩溃后默认重跑 GET 增量为 0，行 success/cdn。
- **Completion Evidence**:
  - models.py L111-L129 期望路径推导；recovery.py L48-L127 崩溃窗口找回+读盘对账+补 derived_path；legacy.py L54-L90 在途 cdn 目标/指纹跳过告警。
  - tests/test_crash_window.py：参数化 3 形态（TR-I-5.1）、migrate 直调跳过（TR-I-5.2）、CLI 迁移（TR-I-5.3）、CLI 默认重跑零 GET（TR-I-5.4）全部 PASSED；全套 59 passed。

## Issue I-6: 无 APP1 的 passthrough JPEG 恢复误判（F11, advisory→采纳）
- **Status**: `completed`
- **Priority**: low
- **Depends On**: None
- **Discovered By**: Review R2
- **Description**:
  - --no-exif 发布的派生件是无 APP1 段的 JPEG，多级崩溃链（pending/failed 行携带 derived_path）下 inspect_derived 访问标签抛 KeyError('APP1')，落入通用异常→None，导致合格件被删除重建（零 CDN、可自愈，但违背对账意图）。
  - 修复：恢复分类先比对 sha256(派生)==raw_digest 判 not_required，再做 EXIF 校验；inspect_derived 增加 SOI 前缀守卫并把 KeyError 与 AttributeError 同视为无标签 JPEG。
- **Acceptance Criteria Addressed**: AC-8
- **Test Requirements**:
  - `rule` TR-I-6.1: passthrough JPEG（字节==raw、无 APP1）+ FAILED 行，恢复后 not_required、文件保留、零 CDN；非 JPEG SOI 的垃圾字节仍判无效删除重建。
- **Completion Evidence**:
  - recovery.py L48-L57 摘要前置；enrich.py L181-L190 SOI 守卫 + KeyError 归类。
  - TR-I-6.1：test_crash_window.py::test_passthrough_jpeg_without_app1_reconciles_not_required PASSED；原垃圾字节重建用例 test_recovery.py::test_reconcile_invalid_published_derived_is_rebuilt 仍 PASSED。

## Issue I-7: 非 raw 字节的损坏 JPEG 不得对账为终态（R3-1, actionable）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: I-6
- **Discovered By**: Review R3
- **Description**:
  - I-6 在 inspect_derived 中把 KeyError/AttributeError/无 DTO 归为 not_required，造成反向过度放行：任何 FFD8 开头但摘要≠raw 的外来/截断字节（SOI 残片、APP1 之前截断的旧下载件）会被恢复对账为终态 not_required 并补写 derived_path，违背不变量「not_required ⇒ 派生字节==raw」；标签段完整但载荷截断的 JPEG 还会被判 success。
  - 可达链路（R3 三轮真实 CLI 复现）：预置截断平铺 jpg → --no-skip-existing 且 exif_parse 注入失败（raw 已提交、行 failed、旧字节保留）→ 默认重跑启动恢复误收编，永久 Skipped、零告警。
  - 修复：inspect_derived 对 JPEG 严格化——新增 _jpeg_structurally_complete（SOI 起步、逐段长度可遍历、SOS 熵码按 FF00/RSTn 填充规则跳过、必须以 EOI 收尾）；结构不全/标签不可读/DTO 空一律返回 None（合法无 APP1 passthrough 已由恢复摘要前置分支保留，F11 不回退）；非 JPEG 保持 not_required。
- **Acceptance Criteria Addressed**: AC-8（兼 AC-6）
- **Test Requirements**:
  - `rule` TR-I-7.1: 分类矩阵——FFD8 残片/FFD8FFD9/SOI+垃圾/10 字节前缀/标签完整载荷截断全部 None；合法 EXIF enriched success；无 APP1 且摘要==raw 仍 not_required。
  - `rule` TR-I-7.2: 崩溃窗口目标为外来截断字节时恢复删除回 pending，随后零额外 GET 从 raw 重建出含 DateTimeOriginal 的完整件。
  - `rule` TR-I-7.3: 真实 CLI：预置截断 jpg→--no-skip-existing+exif_parse:error（旧字节保留、Enrichment failed=1）→默认重跑零新增 GET、终态 success/cdn、可见文件为完整重建件。
- **Completion Evidence**:
  - enrich.py L172-L243：JPEG 段遍历健全性校验 + 严格终态判定。
  - TR-I-7.1/7.2：test_crash_window.py::test_strict_classifier_rejects_nonraw_broken_jpeg；TR-I-7.3：同文件 test_cli_broken_legacy_file_rebuilt_after_injected_failure（真实 loopback 子进程）PASSED；全套 61 passed。
- **R3 advisory 取舍**：R3-2（success 侧结构完整性）已并入本修复；R3-3（memory=None 时不校验日期/GPS）仅在导出 JSON 已不含该在途记录时变弱，删除会永久销毁可零成本重建的文件，保持 advisory 记录不改；F7/F8/F9 维持记录不改。

# Memory 原件/派生件两阶段提交 - Independent Review

- [ ] CP-1: raw 原件内容寻址、字节一致与只读提交
  - **Type**: `rule`
  - **Covers**: AC-1（TR-4.1/4.2/4.3）
  - **Evidence**: Pending

- [ ] CP-2: JPEG 派生件校验发布、标签完整且与 raw 分离
  - **Type**: `rule`
  - **Covers**: AC-2（TR-5.1/5.3）
  - **Evidence**: Pending

- [ ] CP-3: --no-exif 与非 JPEG 原样发布
  - **Type**: `rule`
  - **Covers**: AC-3（TR-5.2）
  - **Evidence**: Pending

- [ ] CP-4: 最小索引原子持久化并驱动零网络续跑
  - **Type**: `rule`
  - **Covers**: AC-4（TR-2.3/3.1/6.1）
  - **Evidence**: Pending

- [ ] CP-5: EXIF 失败隔离、统计口径与免 CDN 重试
  - **Type**: `rule`
  - **Covers**: AC-5（TR-5.4/6.2）
  - **Evidence**: Pending

- [ ] CP-6: raw 提交阶段故障（ENOSPC/崩溃）恢复
  - **Type**: `rule`
  - **Covers**: AC-6（TR-4.3/7.2）
  - **Evidence**: Pending

- [ ] CP-7: 派生写入阶段故障不产生半写发布件
  - **Type**: `rule`
  - **Covers**: AC-7（TR-5.5/7.3）
  - **Evidence**: Pending

- [ ] CP-8: 索引更新阶段崩溃的状态对账
  - **Type**: `rule`
  - **Covers**: AC-8（TR-7.4）
  - **Evidence**: Pending

- [ ] CP-9: 升级前平铺文件默认不被信任
  - **Type**: `rule`
  - **Covers**: AC-9（TR-8.1）
  - **Evidence**: Pending

- [ ] CP-10: 显式迁移保留路径并记录来源未知
  - **Type**: `rule`
  - **Covers**: AC-10（TR-8.2/8.3/8.4）
  - **Evidence**: Pending

- [ ] CP-11: 启动恢复仅清理可识别临时项
  - **Type**: `rule`
  - **Covers**: AC-11（TR-7.1/7.5）
  - **Evidence**: Pending

- [ ] CP-12: 实现结构与故障注入卫生
  - **Type**: `rubric`
  - **Covers**: AC-12（TR-9.4）
  - **Scale**: 1-5
  - **Anchors**: 1 = 逻辑仍耦合单文件、注入污染正常路径；3 = 有拆分但职责交叉、部分故障难模拟；5 = 模块职责单一、注入仅集中 no-op 探针、测试矩阵完整可读
  - **Pass Threshold**: >= 4
  - **Evidence**: Pending

## Review History

### Review R1
- **Result**: `fail`
- **Evidence**: 审查者独立复跑 49/49、24 组故障点×模式子进程矩阵、8 项边界探查。CP-1..CP-10 通过；**CP-11 fail（F-1）**：`is_derived_tmp` 用无锚定子串 `.tmp-` 匹配，输出根任何含该子串的用户点文件（如 `.vacation.photo.tmp-backup.jpg`）会在每次启动恢复被静默删除，违反 FR-7（已实测复现）。CP-12 rubric 4/5。
- **Findings**:
  - F-1 `actionable`（中低）：派生临时项识别过宽 → Issue I-1。
  - F-2 `advisory`（低）：EXIF 校验针对内存缓冲而非磁盘临时件；os.write 单写无写满循环 → 规格 FR-2「重新解析临时件」字面要求，纳入 Issue I-2 一并修复。
  - F-3 `advisory`（低）：raw 去重复用分支不补 chmod，replace→chmod 窗口掉电后永久 0o644 → Issue I-3。
  - F-4 `advisory`（低）：UNKNOWN 迁移行派生件缺失时默认运行静默重下并改写溯源为 cdn → Issue I-4。
  - F-5 `advisory`：index_update_raw 崩溃后无索引行必须重新 GET，符合规格「无条目走网络」，仅需记载取舍（无需改码）。
  - F-6 `advisory`：失败项字节计入 MB（实际传输口径，tasks.md 已如此定义），保留不改。

### Review R2
- **Result**: `fail`
- **Evidence**: 全新独立审查者（不共享 R1 上下文）独立复跑 52/52；自建 42 例谓词形态清扫、磁盘/缓冲发散实验、权限归一化与写打开 hook、真实 loopback 链路、8 故障点×3 模式共 24 场景子进程崩溃矩阵、GPS 半球/mtime/并发 40/迁移回归。**R1 四项修复 F-1..F-4 经独立实验全部真实成立**。CP-1..CP-7、CP-9..CP-11 通过；**CP-8 fail（F10）**：index_update_derived 崩溃后真实形态为「派生件已发布、行 cdn/pending 且 derived_path=None」，恢复仅经 derived_path 找件故不做规格要求的重新对账；随后 --migrate-legacy 把该合格派生件误登记为 unknown（子进程链路实测：行被覆盖、raw_digest 改写为派生字节摘要、raw 孤儿、默认运行永久 Skipped）。CP-12 rubric 4/5。
- **Findings**:
  - F10 `actionable`（中）：崩溃窗口对账缺口 + 迁移可覆盖在途 cdn 行 → Issue I-5。
  - F11 `advisory`（低）：无 APP1 的 passthrough JPEG 在多级崩溃链下 inspect_derived 抛 KeyError 被误删重建 → Issue I-6（恢复摘要前置 + SOI 守卫 + KeyError 归类）。
  - F7 `advisory`（极低）：派生临时件正则 IGNORECASE 比生成端略宽；近乎不可能命中用户文件，记录不改。
  - F8 `advisory`（极低）：png/mov/heic 等域外扩展名的派生临时件不在清扫白名单（仅堆积）；规格域为 jpg/mp4，记录不改。
  - F9 `advisory`（极低，设计使然）：CDN JPEG 自带 GPS 而记录无 Location 时严格校验永久 failed；raw 保留、失败可见、可离线重试，旧代码反为静默保留旧 GPS，记录不改。
  - F-5/F-6 维持 R1 已接受取舍。

### Review R3
- **Result**: `fail`
- **Evidence**: 全新独立审查者复跑 59/59；独立探针验证 I-5（F10）修复真实成立（exit 子进程三形态仅恢复即对账 success/not_required/not_required 且零 GET；直调迁移与 CLI 迁移双路径均不覆盖 cdn 行、raw 不孤儿；坏 GPS/错 DTO/截断/大写扩展名/同秒碰撞/UNKNOWN 豁免等绕过全部正确）；24 场景崩溃矩阵 24/24、40 并发、损坏索引、退出码与测试放水审计均通过。CP-1..CP-7、CP-9..CP-11 通过，CP-12 4/5；**CP-8 fail（R3-1）**：I-6 的 KeyError/无 DTO→not_required 兜底造成反向过度放行——FFD8 开头但摘要≠raw 的外来/截断字节被对账为终态并补写 derived_path，标签完整载荷截断者被判 success；三轮真实 CLI（预置截断 jpg→--no-skip-existing+exif_parse 注入失败→默认重跑）实测永久 Skipped、旧字节保留、零告警。
- **Findings**:
  - R3-1 `actionable`（中）：非 raw 字节损坏 JPEG 误收编 → Issue I-7（JPEG 段遍历完整性 + 严格终态；passthrough 仍由摘要前置保留）。
  - R3-2 `advisory`（低）：success 侧缺结构完整性 sanity → 已并入 I-7 修复。
  - R3-3 `advisory`（低）：memory=None 时不校验 DTO/GPS，仅在 JSON 不再含该记录时变弱；删除派生件会永久销毁可零成本重建的文件，记录不改。
  - F7/F8/F9、F-5/F-6 维持既往取舍。

### Review R4
- **Result**: `pass`
- **Evidence**: 全新独立审查者两次复跑 61/61；I-7 以自构 31 例畸形 JPEG 语料 + 三份合法 JPEG 的全部真前缀/尾截断扫描（千余前缀零误判）、24 格严格分类矩阵、R3 指定三轮真实 CLI 链路（error/enospc/exit 三模式，零新增 GET 重建完整件）独立验证成立；8 故障点×3 模式矩阵 24/24（403 条子断言）、F10 双迁移守卫（含孤立文件/坏字节变体）、并发 8/40、GPS 三半球、损坏索引 fail-closed、.jpeg/mp4/--no-exif/多 SOS 相机风格 JPEG 变体均无绕过且无误杀；测试放水审计零命中（无 skip/xfail/空断言、真实 loopback 子进程）。**CP-1..CP-12 全部 PASS，CP-12 rubric 4/5（>=4 过线）**。
- **Findings**:
  - R4-1 `advisory`（极低，信息性）：无 SOS 的 tags-only 退化 JPEG 可判 success；该形态符合 I-7 明文不变量、不可能由本工具原子发布产生（真实截断必失 EOI/尾段，全前缀扫描已证），仅确定性目标被精心构造的外来文件占据时可达；可选纵深：遍历器要求至少一个 SOS、恢复删除时打印告警。记录不改。
  - F5/F6/F7/F8/F9、R3-3 维持既往 advisory/已接受取舍，经本轮复验无需改动。
- **Pass 依据**: 全部 CP 通过；I-1..I-7 修复逐一经独立实验证真；无 actionable；61/61 全绿。

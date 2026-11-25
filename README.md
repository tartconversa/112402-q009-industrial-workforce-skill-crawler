# 制造岗位采集与培训席位联动（ParkBridge）

一套只依赖 Python 3.10+ 标准库和 SQLite 的采集、技能治理、课程推荐与席位确认项目。它把企业招聘来源、培训机构门户、企业联络员/课程负责人确认、技能治理规则和顾问推荐放在同一条可追溯工作流中。

> 所有 `J1`、`C1`、`U-1`、企业和机构名称均为合成数据。

## 1. 快速开始

无需安装第三方依赖：

```bash
python3 -m unittest discover -s tests -v
```

创建文件数据库并导入仓库自带样例：

```bash
python3 -m parkbridge --db data/parkbridge.sqlite3 init --samples
```

查看 C-1 企业的顾问报告（推荐原因、空余席位、待确认事实、联系标记和历史）：

```bash
python3 -m parkbridge --db data/parkbridge.sqlite3 report --company C-1 --json
```

按技能和开班期查询：

```bash
python3 -m parkbridge --db data/parkbridge.sqlite3 report \
  --skill PLC \
  --start-from 2026-11-01 \
  --start-to 2026-11-30 \
  --json

# 也可直接给开班月份：--period 2026-11
```

导出可重新放回共享表格流程的 UTF-8 CSV：

```bash
python3 -m parkbridge --db data/parkbridge.sqlite3 export-csv --outdir exports
```

离线模拟两个来源的分页 JSON 和课程 CSV 采集：

```bash
rm -f /tmp/parkbridge-demo.sqlite3
python3 -m parkbridge --db /tmp/parkbridge-demo.sqlite3 collect \
  --config examples/local_sources.json
python3 -m parkbridge --db /tmp/parkbridge-demo.sqlite3 recommend DEMO-J1
```

## 2. Python API 主入口

```python
from parkbridge import ParkService
from parkbridge.sample import import_samples

service = ParkService.in_memory()
import_samples(service)

# 顾问查询
report = service.advisor_report(company="C-1", skill="PLC")

# 新建推荐、向机构预留、学员确认
service.create_recommendation("J1", "C1", person="U-7", referral_id="R7")
service.hold_reservation("R7")
service.confirm_referral("R7")
```

文件数据库使用 `ParkService.open("data/parkbridge.sqlite3")`。

## 3. 需求与实现对照

| 要求 | 实现 |
| --- | --- |
| 按来源访问规则采集 | `AccessPolicy` 支持最小请求间隔、每分钟请求数、User-Agent、域名允许列表、超时、429/5xx 指数退避重试和可选 `robots.txt` |
| 分页 | `JsonPagedAdapter` 支持下一页 URL、cursor、page/offset 参数和 `has_more`；`CsvAdapter` 支持完整 CSV 目录 |
| 分页重复/重复发布 | `(source_id, external_id)` 幂等；同页/跨页重复不产生新版本；跨来源相同指纹可用 `detect_duplicates()` / `deduplicate_exact()`，也可人工 `mark_duplicate()` |
| 编辑 | 规范化业务内容的 hash 变化后追加 `record_versions`，并写 `source_edited` 等变化事件 |
| 删除 | 只有完整成功的采集 run 才执行缺失对账，将记录标为 `source_deleted`，不物理删除历史 |
| 临时抓取中断 | 重试后仍失败时 run 标记 `failed/complete=0`，**不执行删除对账**；可 `collect(..., resume=True)` 从记录的 cursor 继续 |
| 来源链接/更新时间/原始版本 | 岗位和课程均有 `source_url`、`source_updated_at`、采集/更新时间、`current_version_id`；`record_versions.raw_json` 保存原始条目，`raw_documents.content` 保存原始分页响应 |
| 企业联络员确认 | `confirm_job(... is_hiring/location/required_skills)` 写字段级确认事实；来源新值先进入 pending 队列 |
| 课程负责人确认 | `confirm_course(... equipment/prerequisites/start/seats/skills)` |
| 技能治理 | `SkillGovernance.add_synonym()`、`add_partial_coverage()`、`add_full_coverage()`；未确认文本相似度只作为治理候选提示 |
| 不能只凭文本相似度判满足 | `SkillGovernance.match_course()` 只采用精确同形、已确认同义词/规范术语和已确认覆盖规则；partial 不会得到 `satisfies=True` |
| 有效岗位推荐 | 撤回、来源删除、重复记录、来源/确认状态为 closed 的岗位不能创建新推荐 |
| 席位容量 | `course_occupancy()` 与 `_assert_capacity()` 统计 `held/confirmed/needs_review` 中仍保留的 `seat_held`；超容量抛 `CapacityExceededError` |
| 同一学员机构唯一 | 首次确认写入 `confirmed_learner_providers(person, provider)`；再确认其他机构抛 `DuplicateLearnerConfirmationError`。同一机构的多门课程允许 |
| 未确认推荐重新核对 | 岗位撤回/停招、技能变化、课程改期/覆盖变化会把 proposed/held 置为 `needs_review`，保留原席位数并写自动复核结果 |
| 已确认报名保留当时依据 | 确认时把岗位要求、课程覆盖/设备/开班期、匹配规则和来源版本写入 `basis_snapshot_json`；后续变化不改快照 |
| 联系标记 | 推荐含 `contact_company`、`contact_learner`、`contact_party`、`needs_contact` |
| 顾问查询 | `advisor_report(company=..., skill=..., start_from=..., start_to=..., person=..., state=...)` 返回推荐原因、空余席位、待确认事实和变化历史 |
| 变化历史 | `change_events` 保存采集、确认、推荐、预留、复核和释放事件；`change_history()` 可按顾问筛选范围追溯 |

## 4. 关键业务状态

推荐状态：

- `proposed`：已推荐，尚未正式占座；
- `held`：已向机构提出并取得预留，计入容量；
- `confirmed`：学员确认，永久保存确认时依据；
- `needs_review`：岗位/课程变化后等待重新核对；
- `mismatch`、`released`、`cancelled`、`rejected`：不继续有效占用。

`needs_review` 记录 `previous_state`。若来自 `held` 且尚未释放，`seat_held=1`，避免系统未经顾问处理就凭空释放机构席位；顾问可调用 `resolve_review(action="keep"|"release"|"confirm")`。

## 5. 采集来源配置

见 `examples/local_sources.json`。真实来源可写成：

```json
{
  "sources": [
    {
      "id": "company-career",
      "kind": "job",
      "adapter": "json",
      "url": "https://example.com/api/jobs",
      "items_path": "data.items",
      "pagination": {
        "mode": "cursor",
        "cursor_param": "cursor",
        "next_cursor_path": "meta.next_cursor",
        "has_more_path": "meta.has_more"
      },
      "field_map": {
        "external_id": "positionId",
        "skills": "requirements.skills",
        "start": "trainingStart"
      },
      "policy": {
        "requests_per_minute": 20,
        "max_retries": 3,
        "backoff_base_seconds": 1,
        "respect_robots_txt": true,
        "allowed_domains": ["example.com"]
      }
    }
  ]
}
```

也可以在 Python 中实现自己的 `SourceAdapter` 或注入自定义 `Transport`，用于企业网站 HTML 适配器、招聘平台 SDK 客户端和离线回放。

## 6. 技能治理原则

- `数控调试`、`CNC调试` 可由治理人员 `add_synonym_pair()` 确认到同一术语；
- `数控调试` 与 `设备参数维护` 即使文本或语义接近，也只有在治理人员登记 `partial` 规则后才显示为“部分覆盖”；
- 部分覆盖会生成推荐理由和缺口，但 `match["satisfies"]` 仍为 `false`，不能在系统中显示为“已满足岗位要求”；
- `difflib` 分数只出现在 `text_similarity_hint`，供治理人员发现候选词，不参与授权判定。

## 7. 主要目录

```text
parkbridge/
  collectors.py   # 访问策略、HTTP/重试/robots、JSON/CSV 分页、原始版本、删除对账
  db.py           # SQLite schema 和连接
  facts.py        # 字段级来源事实、人工确认、pending 队列、事件
  ontology.py     # 术语/同义词/覆盖规则与受控匹配
  service.py      # ParkService：确认、推荐、容量、唯一确认、变更传播、查询
  reporting.py    # CSV 导出
  sample.py       # samples/*.csv 导入
  cli.py          # python -m parkbridge
tests/            # 标准库 unittest 测试
examples/         # 离线来源配置和分页夹具
```

原始样例说明仍适用于 `samples/`：CSV 为 UTF-8，首行列名，空单元格表示来源未提供值。

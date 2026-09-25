"""纯计划构造：手工计划与**真实分解**的生成计划。

## A01：生成计划不再是"把目标复制三遍"

早前的 `build_generated_bundle` 只把用户目标套进三个固定句式
（"理解并解释：<goal>" 等），那不是分解，只是措辞。现在改成确定性的
**目标 → 领域识别 → 技能/里程碑 → 具体任务** 流水线：

1. 对目标做 `casefold + 空白折叠` 归一化，按关键词命中一个领域模板
   （agent/AI、编程、数据库、语言、考试备考），命中不了走通用兜底；
2. 每个领域给出固定的里程碑（技能分组）与**动作 + 具体对象**的任务，
   任务标题来自模板、**绝不回抄目标**；
3. 每个任务补齐 objective / instruction / deliverable /
   acceptance_criteria / evidence_required / prerequisites /
   related_skill_id / estimated_minutes，并按诊断出的每周时长分配预算。

质量不变量由构造保证（模板独立于目标、前置只指向前序任务、总时长按预算
等比缩放），并由 `backend/tests/test_plan_quality.py` 逐条锁定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.ids import new_id
from app.identity.models import Principal
from app.learning.ports import TaskAssessment
from app.product.models import (
    LearningPlan,
    LearningTask,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

#: 生成计划的标识：路由把它写进响应，前端据此区分模板版本。
GENERATED_PLAN_GENERATOR = "template/graph-v2"
#: 任务 → 能力组件映射的版本。与 `graph/v1` 的旧映射点是历史分界线。
TASK_MAPPING_VERSION = "graph-v2/task-v1"
#: 生成计划的评估契约标识（MVP 只有自报）。
ASSESSMENT_CONTRACT_ID = "self-report/v1"
#: 计划总时长下限：无论每周投入多小，都至少留出 3 小时的可执行空间。
MIN_PLAN_MINUTES = 180
#: 无诊断回退预算（路由在无诊断时直接拒绝，这里只是构造侧的兜底）。
DEFAULT_WEEKLY_HOURS = 3


def build_manual_bundle(
    actor: Principal,
    project_id: str,
    goal: str,
    milestone_specs: Sequence[tuple[str, str, Sequence[str]]],
    *,
    now: datetime,
) -> PlanBundle:
    """Build a complete plan while leaving version allocation to the repository."""
    plan_id = new_id("plan")
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    for order, (title, description, task_titles) in enumerate(milestone_specs):
        milestone_id = new_id("mile")
        milestones.append(
            Milestone(milestone_id, actor.tenant_id, project_id, plan_id, order, title, description)
        )
        for task_order, task_title in enumerate(task_titles):
            tasks.append(
                LearningTask(
                    new_id("task"), actor.tenant_id, project_id, milestone_id,
                    task_order, task_title, TaskStatus.PENDING,
                )
            )
    return PlanBundle(
        LearningPlan(plan_id, actor.tenant_id, project_id, 1, goal, PlanStatus.ACTIVE, now),
        tuple(milestones),
        tuple(tasks),
    )


# --------------------------------------------------------------------- 模板定义


@dataclass(frozen=True)
class _TaskSpec:
    """一个任务的模板。`prerequisite_index` 指向同一计划内更早的任务下标。"""

    title: str
    task_type: str
    objective: str
    instruction: str
    deliverable: str
    acceptance_criteria: tuple[str, ...]
    evidence_required: tuple[str, ...]
    related_skill_id: str
    estimated_minutes: int
    prerequisite_index: int | None = None


@dataclass(frozen=True)
class _DomainTemplate:
    domain_id: str
    milestones: tuple[tuple[str, str], ...]
    #: (里程碑下标, 任务模板)。任务顺序即计划顺序。
    tasks: tuple[tuple[int, _TaskSpec], ...]


def _spec(
    title: str,
    task_type: str,
    *,
    objective: str,
    instruction: str,
    deliverable: str,
    acceptance: tuple[str, ...],
    evidence: tuple[str, ...],
    skill: str,
    minutes: int,
    prereq: int | None = None,
) -> _TaskSpec:
    return _TaskSpec(
        title=title,
        task_type=task_type,
        objective=objective,
        instruction=instruction,
        deliverable=deliverable,
        acceptance_criteria=acceptance,
        evidence_required=evidence,
        related_skill_id=skill,
        estimated_minutes=minutes,
        prerequisite_index=prereq,
    )


def _agent_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="agent",
        milestones=(
            ("智能体运行链路", "从模型调用到工具调用的最小闭环"),
            ("工具调用与状态管理", "让智能体可靠地调用外部工具并维护状态"),
            ("落地与评估", "端到端跑通一个真实任务并复盘"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "梳理运行链路的组成模块与数据流",
                    "concept",
                    objective="能说明模型调用、上下文拼装、工具调用在一次运行中的先后关系",
                    instruction="画出一轮运行的时序图，标注每一步的输入与输出",
                    deliverable="一张运行时序图（文字或图片均可）",
                    acceptance=("时序覆盖模型调用与工具调用两段", "每一步都标注输入与输出"),
                    evidence=("运行时序图",),
                    skill="skill:agent.runtime_loop",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "编写一次最小模型调用并解析结构化返回",
                    "practice",
                    objective="亲手完成一次模型调用并把返回解析成结构化数据",
                    instruction="用任意 SDK 发一次请求，把返回解析为 JSON 并打印关键字段",
                    deliverable="可运行的调用脚本与一次成功运行的日志",
                    acceptance=("脚本能独立运行并返回结构化结果", "日志中能看到请求与解析后的字段"),
                    evidence=("调用脚本", "运行日志"),
                    skill="skill:agent.model_call",
                    minutes=40,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "定义工具 schema 并让模型完成一次工具调用",
                    "practice",
                    objective="理解工具调用协议并让模型真实触发一次工具调用",
                    instruction="定义一个工具（schema + 实现），让模型在给定问题上选择并调用它",
                    deliverable="工具定义与一次真实工具调用的记录",
                    acceptance=("工具 schema 与实现一致", "模型输出里出现了工具调用参数"),
                    evidence=("工具定义", "调用记录"),
                    skill="skill:agent.tool_call",
                    minutes=45,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "为多步工具调用加入状态与失败处理",
                    "practice",
                    objective="让多轮、多步的工具调用在失败时可恢复",
                    instruction="为工具调用加上超时、重试与失败回退，并记录每一步状态",
                    deliverable="带状态机的调用流程图与失败场景运行记录",
                    acceptance=("超时与失败都走到显式分支", "状态在步骤之间可追踪"),
                    evidence=("调用流程图", "失败场景日志"),
                    skill="skill:agent.state",
                    minutes=40,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "端到端跑通一个可交付的真实任务",
                    "practice",
                    objective="把一个真实问题交给智能体并得到可用结果",
                    instruction="选一个真实的小任务，记录从输入到最终产出的完整过程",
                    deliverable="端到端运行记录与最终产出",
                    acceptance=("完整链路没有被隐藏的人工干预", "产出确实解决了给定任务"),
                    evidence=("端到端记录", "产出文件"),
                    skill="skill:agent.delivery",
                    minutes=45,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘失败案例并写出改进清单",
                    "reflection",
                    objective="能定位智能体失败的原因并提出改进",
                    instruction="挑一个失败或次优的运行，分析原因并列出 3 条改进",
                    deliverable="一页复盘笔记（含原因与改进项）",
                    acceptance=("至少分析一个失败案例", "改进项具体且可执行"),
                    evidence=("复盘笔记",),
                    skill="skill:agent.reflection",
                    minutes=25,
                    prereq=4,
                ),
            ),
        ),
    )


def _programming_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="programming",
        milestones=(
            ("环境与基础语法", "先让代码在本机跑起来"),
            ("核心抽象与调试", "能组织数据并定位缺陷"),
            ("完成一个小项目", "从需求到可运行产物"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "搭建可复现的本地环境并跑通第一个程序",
                    "concept",
                    objective="能在本机建立隔离的项目环境并运行最小程序",
                    instruction="创建隔离环境、安装依赖、运行一个输出结果的示例并记录命令",
                    deliverable="环境配置说明与首次运行输出",
                    acceptance=("命令可以从零复现", "输出结果与预期一致"),
                    evidence=("环境说明", "运行输出"),
                    skill="skill:programming.environment",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "整理基础语法要点并产出速查表",
                    "concept",
                    objective="能说清变量、分支、循环与函数的语义",
                    instruction="用自己的话整理这四类语法并各配一个可运行片段",
                    deliverable="一页语法速查表（含代码片段）",
                    acceptance=("覆盖变量、分支、循环、函数", "每个片段都能运行"),
                    evidence=("语法速查表",),
                    skill="skill:programming.syntax",
                    minutes=30,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "用合适的数据结构完成增删改查",
                    "practice",
                    objective="能把数据组织成合适的结构并完成基本操作",
                    instruction="选择合适结构存放一组数据，实现增删改查并写测试",
                    deliverable="数据操作代码与对应测试",
                    acceptance=("四类操作都有测试覆盖", "边界输入不再导致崩溃"),
                    evidence=("操作代码", "测试文件"),
                    skill="skill:programming.data",
                    minutes=40,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "定位并修复一个真实缺陷",
                    "practice",
                    objective="能用调试手段定位缺陷根因",
                    instruction="在示例程序中复现一个缺陷，记录排查过程并给出修复",
                    deliverable="缺陷复现步骤与修复提交",
                    acceptance=("缺陷能稳定复现", "修复后不再复现"),
                    evidence=("排查记录", "修复提交"),
                    skill="skill:programming.debug",
                    minutes=35,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "完成一个小项目的端到端实现",
                    "practice",
                    objective="能独立完成一个可运行的小项目",
                    instruction="选一个小需求，完成从设计到运行的完整实现",
                    deliverable="可运行的项目与使用说明",
                    acceptance=("项目能独立运行", "覆盖主要功能路径"),
                    evidence=("项目代码", "运行记录"),
                    skill="skill:programming.project",
                    minutes=40,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘实现过程并列出重构点",
                    "reflection",
                    objective="能识别代码里的坏味道并给出改进",
                    instruction="回看自己的实现，挑出 3 处可改进点并说明理由",
                    deliverable="一页复盘与改进清单",
                    acceptance=("至少列出 3 个改进点", "每条都说明了理由"),
                    evidence=("复盘清单",),
                    skill="skill:programming.reflection",
                    minutes=20,
                    prereq=4,
                ),
            ),
        ),
    )


def _database_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="database",
        milestones=(
            ("数据模型与查询基础", "从表结构到筛选聚合"),
            ("索引与事务", "让查询更快、写入更稳"),
            ("设计与调优", "用执行计划定位瓶颈"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "拆解关系模型与键约束",
                    "concept",
                    objective="能说明表、主键、外键与约束各自的作用",
                    instruction="为一个小场景设计三张表并标注键与约束",
                    deliverable="表结构设计与约束说明",
                    acceptance=("键与约束完整标注", "能解释每张表的职责"),
                    evidence=("表结构设计",),
                    skill="skill:database.model",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "编写覆盖筛选与聚合的查询语句",
                    "practice",
                    objective="能用查询语句完成筛选、分组与聚合",
                    instruction="针对示例数据写出筛选、分组、排序与聚合查询并记录结果",
                    deliverable="查询脚本与结果记录",
                    acceptance=("覆盖筛选、分组、聚合三类", "结果与预期一致"),
                    evidence=("查询脚本", "结果记录"),
                    skill="skill:database.query",
                    minutes=35,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "说明索引的适用场景与代价",
                    "concept",
                    objective="能判断什么时候该给列建索引",
                    instruction="为一条慢查询设计一个索引并说明收益与写入代价",
                    deliverable="索引设计与代价分析",
                    acceptance=("给出明确的适用场景", "说明写入与空间代价"),
                    evidence=("索引分析",),
                    skill="skill:database.index",
                    minutes=30,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "用事务保证一组写操作的一致性",
                    "practice",
                    objective="能用事务边界保证写操作的原子性",
                    instruction="构造一个需要事务的写场景，演示失败回滚与成功提交",
                    deliverable="事务脚本与两次运行记录",
                    acceptance=("失败场景确实回滚", "成功场景完整提交"),
                    evidence=("事务脚本", "运行记录"),
                    skill="skill:database.transaction",
                    minutes=35,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "定位并优化一条慢查询",
                    "practice",
                    objective="能用执行计划定位慢查询的瓶颈",
                    instruction="选一条慢查询，读取执行计划并给出优化前后的对比",
                    deliverable="执行计划对比与优化说明",
                    acceptance=("给出优化前后对比", "说明瓶颈的具体原因"),
                    evidence=("执行计划", "优化说明"),
                    skill="skill:database.tuning",
                    minutes=40,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘一次设计取舍并记录结论",
                    "reflection",
                    objective="能复盘设计与优化中的取舍",
                    instruction="回看上面的设计，记录一次取舍的背景、利弊与结论",
                    deliverable="一页取舍复盘",
                    acceptance=("说明背景与取舍", "给出后续改进方向"),
                    evidence=("复盘笔记",),
                    skill="skill:database.reflection",
                    minutes=20,
                    prereq=4,
                ),
            ),
        ),
    )


def _language_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="language",
        milestones=(
            ("框架与输入", "先把语法骨架和词汇立起来"),
            ("听读与输出训练", "从输入到主动输出"),
            ("真实场景应用", "在真实沟通里用出来"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "梳理核心语法框架并产出句型清单",
                    "concept",
                    objective="能说清基本句型与时态结构",
                    instruction="整理 5 个基本句型，并各造两个例句",
                    deliverable="句型清单与例句",
                    acceptance=("覆盖基本句型", "例句符合目标结构"),
                    evidence=("句型清单",),
                    skill="skill:language.grammar",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "掌握一组高频词汇并用于造句",
                    "practice",
                    objective="能主动使用一批高频词汇",
                    instruction="选 30 个高频词，各造一个句子并朗读记录",
                    deliverable="词汇表与造句记录",
                    acceptance=("词汇量不少于 30", "每个句子用词正确"),
                    evidence=("词汇表", "造句记录"),
                    skill="skill:language.vocabulary",
                    minutes=30,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "完成一段听力精听并复述要点",
                    "practice",
                    objective="能听懂一段材料并复述主要信息",
                    instruction="选一段 1-2 分钟的材料做精听，并写下要点",
                    deliverable="精听笔记与要点复述",
                    acceptance=("要点覆盖主要内容", "标注出听不出来的部分"),
                    evidence=("精听笔记",),
                    skill="skill:language.listening",
                    minutes=30,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "完成一次限时口语或写作输出",
                    "practice",
                    objective="能在有限时间内完成一次输出",
                    instruction="就一个话题限时输出一段口语或短文，并自我修订",
                    deliverable="初稿与修订稿",
                    acceptance=("字数或时长达到目标", "修订稿有明显改进"),
                    evidence=("初稿", "修订稿"),
                    skill="skill:language.output",
                    minutes=35,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "完成一次真实场景沟通",
                    "practice",
                    objective="能在真实场景中完成一次沟通",
                    instruction="用目标语言完成一次真实交流并记录过程",
                    deliverable="交流记录与问题清单",
                    acceptance=("完成一次完整交流", "记录下遇到的表达困难"),
                    evidence=("交流记录",),
                    skill="skill:language.scenario",
                    minutes=30,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘错误并制定下一阶段计划",
                    "reflection",
                    objective="能从错误中提炼改进点",
                    instruction="整理这一阶段的错误，列出 3 条改进并排下一阶段安排",
                    deliverable="错误复盘与下一阶段计划",
                    acceptance=("至少 3 条改进", "每条都给出可执行的下一步"),
                    evidence=("复盘笔记",),
                    skill="skill:language.reflection",
                    minutes=20,
                    prereq=4,
                ),
            ),
        ),
    )


def _exam_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="exam",
        milestones=(
            ("考纲与知识盘点", "先把范围与优先级定下来"),
            ("专项训练", "用练习和错题补齐薄弱"),
            ("模拟与查漏", "按考试节奏演练并调整"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "拆解考纲并把知识点分成必会与选会",
                    "concept",
                    objective="能根据考纲排出知识点的优先级",
                    instruction="梳理考纲条目，逐条标注掌握状态与优先级",
                    deliverable="带优先级的知识清单",
                    acceptance=("覆盖主要考纲条目", "每一项都标注了掌握状态"),
                    evidence=("知识清单",),
                    skill="skill:exam.syllabus",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "针对薄弱模块做一组专项练习",
                    "practice",
                    objective="能通过专项练习补齐薄弱环节",
                    instruction="选一个薄弱模块做一组练习并订正错题",
                    deliverable="练习记录与订正说明",
                    acceptance=("练习量达到预定目标", "错题完成订正"),
                    evidence=("练习记录", "订正说明"),
                    skill="skill:exam.drill",
                    minutes=40,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "整理高频错题并建立错题本",
                    "practice",
                    objective="能分类整理错题并定位错因",
                    instruction="把错题按知识点分类，并为每题标注错因",
                    deliverable="分类清晰的错题本",
                    acceptance=("按知识点完成分类", "每题都标注了错因"),
                    evidence=("错题本",),
                    skill="skill:exam.errors",
                    minutes=30,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "完成一次限时模拟并评分",
                    "practice",
                    objective="能按考试节奏完成一次模拟",
                    instruction="限时完成一套模拟题，并按下评分标准自评",
                    deliverable="模拟作答与得分记录",
                    acceptance=("在限定时间内完成", "按标准给出自评得分"),
                    evidence=("模拟作答", "得分记录"),
                    skill="skill:exam.mock",
                    minutes=45,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "分析失分点并调整复习顺序",
                    "reflection",
                    objective="能根据失分点调整复习计划",
                    instruction="分析模拟的失分点，重排下一阶段的复习顺序",
                    deliverable="失分分析与调整后的计划",
                    acceptance=("分析到具体的知识点", "给出新的复习顺序"),
                    evidence=("失分分析",),
                    skill="skill:exam.plan",
                    minutes=25,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘整轮备考并固化有效方法",
                    "reflection",
                    objective="能总结出适合自己的备考方法",
                    instruction="回看整轮过程，写下 3 条有效做法与 1 条要停止的做法",
                    deliverable="方法复盘笔记",
                    acceptance=("至少 3 条有效做法", "明确一条要停止的做法"),
                    evidence=("方法复盘",),
                    skill="skill:exam.reflection",
                    minutes=20,
                    prereq=4,
                ),
            ),
        ),
    )


def _generic_template() -> _DomainTemplate:
    return _DomainTemplate(
        domain_id="generic",
        milestones=(
            ("建立基础认知", "先把概念与资料立起来"),
            ("动手练习", "从最小练习到综合应用"),
            ("复盘与迁移", "把所学用到新情境"),
        ),
        tasks=(
            (
                0,
                _spec(
                    "厘清核心概念并画出知识地图",
                    "concept",
                    objective="能说清这个领域的核心概念与它们的关系",
                    instruction="梳理核心概念，并画出它们之间的关系图",
                    deliverable="一张知识地图",
                    acceptance=("覆盖主要概念", "标出概念之间的关系"),
                    evidence=("知识地图",),
                    skill="skill:generic.concepts",
                    minutes=25,
                ),
            ),
            (
                0,
                _spec(
                    "筛选学习资料并标注重点",
                    "concept",
                    objective="能筛选高质量资料并标注重点",
                    instruction="收集 3 份资料，逐份标注重点与疑问",
                    deliverable="资料清单与标注",
                    acceptance=("资料不少于 3 份", "每一份都标注了重点"),
                    evidence=("资料清单",),
                    skill="skill:generic.resources",
                    minutes=30,
                    prereq=0,
                ),
            ),
            (
                1,
                _spec(
                    "完成一次最小可交付的练习",
                    "practice",
                    objective="能产出一次可交付的练习成果",
                    instruction="设计并完成一个最小练习，记录完整过程",
                    deliverable="练习成果与过程记录",
                    acceptance=("成果可验证", "过程可复现"),
                    evidence=("练习成果", "过程记录"),
                    skill="skill:generic.practice",
                    minutes=40,
                    prereq=1,
                ),
            ),
            (
                1,
                _spec(
                    "针对一个难点做专项突破",
                    "practice",
                    objective="能针对一个难点做针对性训练",
                    instruction="选一个难点拆成小步骤，逐步攻克并记录",
                    deliverable="专项突破记录",
                    acceptance=("难点被拆成可执行的步骤", "每一步都有完成标志"),
                    evidence=("突破记录",),
                    skill="skill:generic.breakthrough",
                    minutes=35,
                    prereq=2,
                ),
            ),
            (
                2,
                _spec(
                    "完成一次跨情境的综合应用",
                    "practice",
                    objective="能把所学迁移到一个新情境",
                    instruction="在一个新情境里综合运用所学并产出结果",
                    deliverable="综合应用产出",
                    acceptance=("用到了至少两个知识点", "产出解决了新情境的问题"),
                    evidence=("应用产出",),
                    skill="skill:generic.application",
                    minutes=35,
                    prereq=3,
                ),
            ),
            (
                2,
                _spec(
                    "复盘学习过程并调整后续安排",
                    "reflection",
                    objective="能复盘并调整后续学习安排",
                    instruction="回看整个过程，列出有效做法与下一步安排",
                    deliverable="复盘与后续安排",
                    acceptance=("至少 2 条有效做法", "给出清晰的下一步安排"),
                    evidence=("复盘笔记",),
                    skill="skill:generic.reflection",
                    minutes=20,
                    prereq=4,
                ),
            ),
        ),
    )


#: 领域关键词。顺序即优先级：更具体、更不会误伤的排在前面。
#: 全部小写 —— 与归一化后的目标（casefold）比对。
_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "agent",
        ("agent", "智能体", "大模型", "llm", "工具调用", "模型调用", "prompt 工程"),
    ),
    (
        "database",
        ("数据库", "sql", "mysql", "postgres", "postgresql", "索引", "事务", "慢查询"),
    ),
    (
        "programming",
        ("编程", "代码", "程序", "python", "算法", "软件", "开发"),
    ),
    (
        "exam",
        ("考试", "备考", "雅思", "托福", "考证", "考研", "高考", "资格证", "四六级"),
    ),
    (
        "language",
        ("英语", "日语", "韩语", "口语", "单词", "语法", "听力", "写作", "语言"),
    ),
)

_TEMPLATE_BUILDERS = {
    "agent": _agent_template,
    "programming": _programming_template,
    "database": _database_template,
    "language": _language_template,
    "exam": _exam_template,
    "generic": _generic_template,
}


def normalize_goal(goal: str) -> str:
    """目标归一化：casefold + 空白折叠。用于领域识别与"标题不得回抄目标"的判定。"""
    return " ".join(goal.casefold().split())


def select_domain(goal: str) -> str:
    """按关键词确定性选领域；命中不了走通用兜底。"""
    normalized = normalize_goal(goal)
    for domain_id, keywords in _DOMAIN_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return domain_id
    return "generic"


def _allocate_minutes(base: Sequence[int], budget: int) -> list[int]:
    """把模板权重等比缩放进预算：每项至少 1 分钟，总和不超过预算。

    用整除向下取整：`sum(floor(m * budget / total)) <= budget` 恒成立，
    所以"小于等于预算"这条不变量由算术保证，而不是靠事后裁剪。
    """
    total = sum(base)
    if total <= budget:
        return list(base)
    return [max(1, (minutes * budget) // total) for minutes in base]


def build_generated_bundle(
    actor: Principal,
    project_id: str,
    goal: str,
    *,
    weekly_hours: int,
    now: datetime,
) -> tuple[PlanBundle, tuple[TaskAssessment, ...]]:
    """按诊断出的每周时长，把一个目标真实分解成里程碑与具体任务。"""
    template = _TEMPLATE_BUILDERS[select_domain(goal)]()
    budget = max(int(weekly_hours) * 60, MIN_PLAN_MINUTES)
    minutes = _allocate_minutes(
        [spec.estimated_minutes for _, spec in template.tasks], budget
    )

    plan_id = new_id("plan")
    milestone_ids: list[str] = []
    milestones: list[Milestone] = []
    for order, (title, description) in enumerate(template.milestones):
        milestone_id = new_id("mile")
        milestone_ids.append(milestone_id)
        milestones.append(
            Milestone(milestone_id, actor.tenant_id, project_id, plan_id, order, title, description)
        )

    task_ids = [new_id("task") for _ in template.tasks]
    order_within_milestone: dict[int, int] = {}
    tasks: list[LearningTask] = []
    assessments: list[TaskAssessment] = []
    for index, (milestone_index, spec) in enumerate(template.tasks):
        position = order_within_milestone.get(milestone_index, 0)
        order_within_milestone[milestone_index] = position + 1
        prerequisites = (
            () if spec.prerequisite_index is None else (task_ids[spec.prerequisite_index],)
        )
        tasks.append(
            LearningTask(
                task_id=task_ids[index],
                tenant_id=actor.tenant_id,
                project_id=project_id,
                milestone_id=milestone_ids[milestone_index],
                order_index=position,
                title=spec.title,
                status=TaskStatus.PENDING,
                objective=spec.objective,
                instruction=spec.instruction,
                task_type=spec.task_type,
                estimated_minutes=minutes[index],
                deliverable=spec.deliverable,
                acceptance_criteria=spec.acceptance_criteria,
                evidence_required=spec.evidence_required,
                prerequisites=prerequisites,
                related_skill_id=spec.related_skill_id,
            )
        )
        # 组件取自 task_type 闭集（concept/practice/reflection）。
        assessments.append(
            TaskAssessment(
                new_id("asm"),
                task_ids[index],
                spec.task_type,
                ASSESSMENT_CONTRACT_ID,
                TASK_MAPPING_VERSION,
            )
        )

    return (
        PlanBundle(
            LearningPlan(plan_id, actor.tenant_id, project_id, 1, goal, PlanStatus.ACTIVE, now),
            tuple(milestones),
            tuple(tasks),
        ),
        tuple(assessments),
    )

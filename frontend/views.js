/* Presentational workbench views. */
(function () {
  "use strict";
  const h = React.createElement;

  function LoadingScreen() {
    return h("div", { className: "center-screen" }, h("div", { className: "loading-mark", "aria-label": "正在加载" }, "学"), h("p", null, "正在打开学习空间"));
  }

  function AuthScreen({ mode, setMode, token, setToken, credentials, setCredentials, onInvite, onPassword, busy, error }) {
    return h("main", { className: "auth-screen" }, h("section", { className: "auth-panel" },
      h("div", { className: "brand large" }, h("span", { className: "brand-mark", "aria-hidden": true }, "学"), h("span", null, "学习工作台")),
      h("h1", null, "把下一步学什么，变成今天能完成的事"),
      h("p", { className: "auth-copy" }, mode === "invite" ? "用邀请访问码进入你的学习空间。" : "使用用户名和密码进入学习空间。"),
      h("div", { className: "auth-switch", role: "tablist", "aria-label": "认证方式" }, [
        ["invite", "邀请码"], ["login", "密码登录"], ["register", "注册账号"],
      ].map(([value, label]) => h("button", { key: value, type: "button", role: "tab", "aria-selected": mode === value, className: mode === value ? "active" : "", onClick: () => {
        setCredentials({ username: "", password: "" });
        setToken("");
        setMode(value);
      } }, label))),
      error && h("div", { className: "alert alert-error", role: "alert" }, error),
      mode === "invite" ? h("form", { onSubmit: onInvite, className: "auth-form" },
        h("label", { htmlFor: "invite-token" }, "邀请访问码"),
        h("input", { id: "invite-token", value: token, onChange: (event) => setToken(event.target.value), autoComplete: "one-time-code", placeholder: "粘贴访问码", disabled: busy }),
        h("button", { className: "primary-button", disabled: busy || !token.trim() }, busy ? "正在进入…" : "进入学习空间")
      ) : h("form", { onSubmit: onPassword, className: "auth-form" },
        h("label", { htmlFor: "auth-username" }, "用户名"),
        h("input", { id: "auth-username", value: credentials.username, onChange: (event) => setCredentials({ ...credentials, username: event.target.value }), autoComplete: "username", minLength: 1, maxLength: 16, placeholder: "1–16 个字符", disabled: busy }),
        h("label", { htmlFor: "auth-password" }, "密码"),
        h("input", { id: "auth-password", type: "password", value: credentials.password, onChange: (event) => setCredentials({ ...credentials, password: event.target.value }), autoComplete: mode === "register" ? "new-password" : "current-password", minLength: 12, maxLength: 128, placeholder: "至少 12 个字符", disabled: busy }),
        mode === "register" && h("p", { className: "field-hint" }, "首版不提供邮箱找回；请妥善保存密码。"),
        h("button", { className: "primary-button", disabled: busy || !credentials.username.trim() || !credentials.password }, busy ? "处理中…" : mode === "register" ? "创建账号" : "登录")
      )
    ));
  }

  function ProjectRail({ projects, projectId, setProjectId, newProject, setNewProject, createProject, loading, projectFormOpen, setProjectFormOpen }) {
    return h("aside", { className: "project-rail" },
      h("div", { className: "rail-heading" }, h("span", null, "项目"), h("span", { className: "count" }, projects.length)),
      h("nav", { className: "project-list", "aria-label": "学习项目" }, projects.length ? projects.map((item) => h("button", { key: item.project_id, className: `project-item ${item.project_id === projectId ? "selected" : ""}`, onClick: () => setProjectId(item.project_id) }, h("span", { className: "project-dot", "aria-hidden": true }, ""), h("span", null, item.name))) : h("p", { className: "rail-empty" }, "还没有项目")),
      h("details", { className: "project-create", open: projectFormOpen, onToggle: (event) => setProjectFormOpen(event.currentTarget.open) },
        h("summary", null, "新建项目"),
        h("form", { className: "project-form", onSubmit: createProject },
          h("label", { htmlFor: "project-name" }, "项目名称"),
          h("input", { id: "project-name", value: newProject.name, onChange: (event) => setNewProject({ ...newProject, name: event.target.value }), placeholder: "例如：Agent 工程基础" }),
          h("label", { htmlFor: "project-goal" }, "项目目标（可选）"),
          h("textarea", { id: "project-goal", value: newProject.goal, onChange: (event) => setNewProject({ ...newProject, goal: event.target.value }), placeholder: "项目目标（可选）", rows: 3 }),
          h("button", { className: "primary-button compact", disabled: loading || !newProject.name.trim() }, "+ 创建项目")
        )
      )
    );
  }

  function WorkspaceTabs({ view, setView }) {
    return h("nav", { className: "tabs", "aria-label": "项目视图" }, [
      ["conversation", "会话"], ["plan", "计划"], ["sources", "资料"],
    ].map(([value, label]) => h("button", { key: value, className: view === value ? "active" : "", onClick: () => setView(value) }, label)));
  }

  function EmptyProject({ onFocus }) {
    return h("section", { className: "empty-project" }, h("div", { className: "empty-glyph", "aria-hidden": true }, "＋"), h("h2", null, "先创建一个学习项目"), h("p", null, "项目会保存你的会话、资料和计划。"), h("button", { className: "secondary-button", onClick: onFocus }, "创建项目"));
  }

  function MainView(props) {
    if (props.view === "plan") return h(PlanView, props);
    if (props.view === "sources") return h(SourcesView, props);
    return h(ConversationView, props);
  }

  function ConversationView({ conversations, conversationId, setConversationId, currentConversation, messages, question, setQuestion, askQuestion, loading, activeRun }) {
    return h("section", { className: "content-section conversation-view" },
      conversations.length > 0 && h("div", { className: "conversation-picker" }, h("label", { htmlFor: "conversation-select" }, "会话"), h("select", { id: "conversation-select", value: conversationId, onChange: (event) => setConversationId(event.target.value) }, conversations.map((item) => h("option", { key: item.conversation_id, value: item.conversation_id }, item.title || "未命名会话")))),
      !currentConversation ? h("div", { className: "empty-state" }, h("h2", null, "还没有会话"), h("p", null, "创建一个会话后开始提问。")) : h(React.Fragment, null,
        h("div", { className: "message-stream" }, messages.length ? messages.map((message) => h(MessageBubble, { key: message.message_id, message })) : h("div", { className: "empty-state compact-empty" }, h("h2", null, "从一个问题开始"), h("p", null, "你的提问和回答会按时间顺序保存在这里。"))),
        activeRun && h(RunStrip, { run: activeRun }),
        h("form", { className: "question-form", onSubmit: askQuestion }, h("textarea", { value: question, onChange: (event) => setQuestion(event.target.value), placeholder: "你现在想弄懂什么？", rows: 3, "aria-label": "输入问题", disabled: loading }), h("div", { className: "question-actions" }, h("span", { className: "field-hint" }, "回答会基于当前项目资料"), h("button", { className: "primary-button", disabled: loading || !question.trim() }, loading ? "处理中…" : "发送问题")))
      )
    );
  }

  function MessageBubble({ message }) {
    return h("article", { className: `message ${message.role}` }, h("div", { className: "message-meta" }, message.role === "user" ? "你" : message.role === "assistant" ? "学习助手" : "系统"), h("div", { className: "message-content" }, message.content));
  }

  function RunStrip({ run }) {
    const labels = { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败", reconciliation_required: "待对账" };
    return h("div", { className: `run-strip ${run.status}` }, h(StatusDot, { status: run.status }), h("span", null, labels[run.status] || run.status), run.error_detail && h("span", { className: "run-detail" }, run.error_detail));
  }

  function PlanView({ plan, planForm, setPlanForm, savePlan, loading }) {
    const milestones = plan?.milestones || [];
    const tasks = plan?.tasks || [];
    return h("section", { className: "content-section form-section" },
      h("div", { className: "section-title" },
        h("div", null,
          h("p", { className: "eyebrow" }, "学习路径"),
          h("h2", null, plan ? `第 ${plan.plan?.version || 1} 版计划` : "还没有计划")
        )
      ),
      !plan && h("form", { className: "plan-form", onSubmit: savePlan },
        h("label", { htmlFor: "plan-goal" }, "学习目标"),
        h("textarea", { id: "plan-goal", value: planForm.goal, onChange: (event) => setPlanForm({ ...planForm, goal: event.target.value }), rows: 4, placeholder: "完成后你希望能独立做什么？" }),
        h("label", { htmlFor: "plan-milestone" }, "第一个里程碑"),
        h("input", { id: "plan-milestone", value: planForm.milestone, onChange: (event) => setPlanForm({ ...planForm, milestone: event.target.value }), placeholder: "例如：能解释 Agent harness 的关键边界" }),
        h("button", { className: "primary-button", disabled: loading || !planForm.goal.trim() || !planForm.milestone.trim() }, loading ? "创建中…" : "创建计划")
      ),
      plan && h("p", { className: "read-only-note" }, "当前计划为只读版本；后续编辑会保留任务身份和学习证据。"),
      plan && h("div", { className: "plan-summary" },
        milestones.map((milestone) => h("div", { className: "milestone-row", key: milestone.milestone_id }, h(StatusDot, { status: "queued" }), h("span", null, milestone.title))),
        tasks.map((task) => h("div", { className: "task-row", key: task.task_id }, h("span", { className: "task-check", "aria-hidden": true }, "□"), h("span", null, task.title)))
      )
    );
  }

  function SourcesView({ sources, ingestionJobs, candidates, acquisitionJobs, sourceForm, setSourceForm, saveSource, candidateForm, setCandidateForm, saveCandidate, sourceSearchQuery, setSourceSearchQuery, searchSourceCandidates, selectCandidate, loading }) {
    const labels = { queued: "排队中", processing: "处理中", succeeded: "已处理", failed: "处理失败" };
    const acquisitionLabels = { queued: "等待下载", running: "下载中", succeeded: "已下载", failed: "下载失败", unknown: "待确认" };
    const latestJobs = new Map();
    (ingestionJobs || []).forEach((job) => {
      if (!latestJobs.has(job.source_id)) latestJobs.set(job.source_id, job);
    });
    return h("section", { className: "content-section form-section" },
      h("div", { className: "section-title" }, h("div", null, h("p", { className: "eyebrow" }, "项目资料"), h("h2", null, `${sources.length} 份资料`))),
      h("form", { className: "source-search-form", onSubmit: searchSourceCandidates },
        h("label", { htmlFor: "source-search-query" }, "搜索公开资料"),
        h("div", { className: "source-search-controls" },
          h("input", { id: "source-search-query", value: sourceSearchQuery, onChange: (event) => setSourceSearchQuery(event.target.value), maxLength: 2000, placeholder: "输入主题或关键词", required: true }),
          h("button", { className: "primary-button", disabled: loading || !sourceSearchQuery.trim() }, loading ? "搜索中…" : "搜索")
        )
      ),
      h("form", { className: "source-form candidate-form", onSubmit: saveCandidate },
        h("label", { htmlFor: "candidate-url" }, "资料网址"),
        h("input", { id: "candidate-url", type: "url", value: candidateForm.url, onChange: (event) => setCandidateForm({ ...candidateForm, url: event.target.value }), placeholder: "https://example.com/guide", required: true }),
        h("label", { htmlFor: "candidate-title" }, "候选标题"),
        h("input", { id: "candidate-title", value: candidateForm.title, onChange: (event) => setCandidateForm({ ...candidateForm, title: event.target.value }), placeholder: "例如：事务入门指南", required: true }),
        h("label", { htmlFor: "candidate-snippet" }, "摘要（可选）"),
        h("textarea", { id: "candidate-snippet", value: candidateForm.snippet, onChange: (event) => setCandidateForm({ ...candidateForm, snippet: event.target.value }), rows: 3, placeholder: "帮助你确认来源，不会代替正文" }),
        h("button", { className: "primary-button", disabled: loading || !candidateForm.url.trim() || !candidateForm.title.trim() }, loading ? "登记中…" : "登记候选资料")
      ),
      h("div", { className: "source-list candidate-list" }, candidates?.length ? candidates.map((candidate) => {
        const acquisition = (acquisitionJobs || []).find((job) => job.candidate_id === candidate.candidate_id);
        const status = acquisition ? acquisitionLabels[acquisition.status] || acquisition.status : candidate.status === "selected" ? "已授权" : "待确认";
        return h("div", { className: "source-row candidate-row", key: candidate.candidate_id },
          h("div", null,
            h("strong", null, candidate.title),
            h("a", { href: candidate.url, target: "_blank", rel: "noreferrer" }, candidate.source_domain),
            candidate.snippet && h("span", null, candidate.snippet)
          ),
          h("div", { className: "candidate-actions" },
            h("span", { className: `source-status ${acquisition?.status || candidate.status}` }, status),
            candidate.status === "discovered" && h("button", { className: "secondary-button compact", type: "button", onClick: () => selectCandidate(candidate), disabled: loading }, "确认下载")
          )
        );
      }) : h("div", { className: "empty-state compact-empty" }, h("h2", null, "还没有候选资料"), h("p", null, "粘贴一个公开网址，确认后才会进入下载队列。"))),
      h("div", { className: "source-divider", "aria-hidden": true }),
      h("h2", { className: "subsection-title" }, "上传本地资料"),
      h("form", { className: "source-form", onSubmit: saveSource }, h("label", { htmlFor: "source-display" }, "资料名称"), h("input", { id: "source-display", value: sourceForm.displayName, onChange: (event) => setSourceForm({ ...sourceForm, displayName: event.target.value }), placeholder: "例如：Agent 设计笔记" }), h("label", { htmlFor: "source-title" }, "文档标题"), h("input", { id: "source-title", value: sourceForm.title, onChange: (event) => setSourceForm({ ...sourceForm, title: event.target.value }), placeholder: "例如：工具调用边界" }), h("label", { htmlFor: "source-content" }, "正文"), h("textarea", { id: "source-content", value: sourceForm.content, onChange: (event) => setSourceForm({ ...sourceForm, content: event.target.value }), rows: 8, placeholder: "粘贴 Markdown 或纯文本" }), h("button", { className: "primary-button", disabled: loading || !sourceForm.content.trim() }, loading ? "登记中…" : "登记资料")),
      h("div", { className: "source-list" }, sources.length ? sources.map((source) => {
      const job = latestJobs.get(source.source_id);
      const status = job ? labels[job.status] || job.status : "已登记";
      return h("div", { className: "source-row", key: source.source_id }, h("div", null, h("strong", null, source.display_name), h("span", null, source.media_type || "未标注类型"), job && h("span", null, `版本 ${job.document_id}`)), h("span", { className: `source-status ${job?.status || "registered"}` }, status));
      }) : h("div", { className: "empty-state compact-empty" }, h("h2", null, "还没有资料"), h("p", null, "登记资料后，回答可以带上原文引用。")))
    );
  }

  function EvidenceRail({ activeRun, sources, plan, view, onReadCitation, citationReading }) {
    const statusLabel = activeRun?.status === "succeeded"
      ? activeRun.grounding === "sourced" ? "来源已核验" : "一般性回答（未核验来源）"
        : activeRun?.status === "reconciliation_required"
        ? "需要对账"
        : "运行状态";
    const rewriteStatus = activeRun?.routing_decision?.query_rewrite_status;
    const routeLabel = rewriteStatus === "applied"
      ? "本地查询改写 · 云端教学回答"
      : rewriteStatus === "fallback"
        ? "关键词检索降级 · 云端教学回答"
        : rewriteStatus === "disabled"
          ? "关键词检索 · 云端教学回答"
          : "";
    const evidence = activeRun
      ? h(React.Fragment, null,
        h("div", { className: "evidence-status" }, h(StatusDot, { status: activeRun.status }), h("strong", null, statusLabel)),
        routeLabel && h("p", { className: "routing-mode" }, routeLabel),
        activeRun.grounding && h("p", { className: "grounding" }, activeRun.grounding === "sourced" ? "回答含有已核验来源" : "回答没有通过核验的来源"),
        activeRun.citations?.length
          ? h("div", { className: "citation-list" }, activeRun.citations.map((citation, index) => {
            const key = `${citation.document_id}:${citation.span_start}:${citation.span_end}`;
            const reading = citationReading?.key === key ? citationReading : null;
            return h("div", { className: "citation", key: `${citation.source_id}-${index}` },
              h("span", { className: "citation-index" }, String(index + 1).padStart(2, "0")),
              h("div", null,
                h("strong", null, sources.find((source) => source.source_id === citation.source_id)?.display_name || citation.source_id),
                h("span", null, `第 ${citation.span_start}–${citation.span_end} 字`),
                h("code", null, citation.content_hash),
                h("button", { className: "secondary-button compact citation-button", onClick: () => onReadCitation(citation), disabled: reading?.loading }, reading?.loading ? "读取中…" : "读取原文"),
                reading?.error && h("span", { className: "citation-error" }, reading.error),
                reading?.result && h("blockquote", { className: "citation-content" }, reading.result.content)
              )
            );
          }))
          : h("div", { className: "empty-rail" }, "完成回答后，已核验的引用会出现在这里。")
      )
      : h("div", { className: "empty-rail" }, sources.length ? `${sources.length} 份资料已登记，等待回答引用。` : plan ? "先登记资料，回答才有可回读的证据。" : "选择一个项目开始。");
    return h("aside", { className: "evidence-rail" }, h("div", { className: "rail-heading" }, h("span", null, view === "sources" ? "资料状态" : "证据")), evidence);
  }

  function StatusDot({ status }) {
    return h("span", { className: `status-dot ${status}`, "aria-hidden": true });
  }

  window.StudyViews = { LoadingScreen, AuthScreen, ProjectRail, WorkspaceTabs, EmptyProject, MainView, EvidenceRail };
})();

/* global React, ReactDOM */

(function () {
  "use strict";

  const { useCallback, useEffect, useMemo, useState } = React;
  const h = React.createElement;

  class ApiError extends Error {
    constructor(status, payload) {
      super(payload && payload.message ? payload.message : "请求未完成");
      this.status = status;
      this.payload = payload || {};
    }
  }

  const writeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  const requestKey = (scope) => `${scope}-${crypto.randomUUID()}`;

  async function api(method, path, body, options) {
    const settings = options || {};
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (writeMethods.has(method) && settings.idempotent !== false) {
      headers["Idempotency-Key"] = settings.key || requestKey("web");
    }
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (error) {
      payload = { message: text || "服务器返回了无法读取的响应" };
    }
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }

  function errorText(error) {
    if (!(error instanceof ApiError)) return "网络连接失败，请稍后重试";
    const code = error.payload && error.payload.code;
    if (code === "TEACHING_PROVIDER_DISABLED") return "教学服务尚未启用";
    if (code === "RECONCILIATION_REQUIRED") return "这次请求的结果待对账，暂时不能自动重试";
    if (code === "BUDGET_EXCEEDED") return "项目预算不足，请减少资料或稍后再试";
    if (code === "CSRF_DENIED") return "页面来源校验失败，请刷新后重试";
    if (error.status === 401) return "会话已失效，请重新兑换邀请";
    return (error.payload && error.payload.message) || `请求失败（${error.status}）`;
  }

  function App() {
    const [user, setUser] = useState(null);
    const [authToken, setAuthToken] = useState("");
    const [authBusy, setAuthBusy] = useState(true);
    const [projects, setProjects] = useState([]);
    const [projectId, setProjectId] = useState("");
    const [project, setProject] = useState(null);
    const [conversations, setConversations] = useState([]);
    const [conversationId, setConversationId] = useState("");
    const [messages, setMessages] = useState([]);
    const [plan, setPlan] = useState(null);
    const [sources, setSources] = useState([]);
    const [activeRun, setActiveRun] = useState(null);
    const [view, setView] = useState("conversation");
    const [notice, setNotice] = useState("");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);

    const [newProject, setNewProject] = useState({ name: "", goal: "" });
    const [newConversation, setNewConversation] = useState("");
    const [question, setQuestion] = useState("");
    const [planForm, setPlanForm] = useState({ goal: "", milestone: "" });
    const [sourceForm, setSourceForm] = useState({
      displayName: "",
      title: "",
      content: "",
      mediaType: "text/markdown",
    });

    const currentConversation = useMemo(
      () => conversations.find((item) => item.conversation_id === conversationId) || null,
      [conversations, conversationId]
    );

    const refreshProjects = useCallback(async () => {
      const result = await api("GET", "/projects");
      const next = result.projects || [];
      setProjects(next);
      setProjectId((previous) => (next.some((item) => item.project_id === previous) ? previous : next[0]?.project_id || ""));
      return next;
    }, []);

    const refreshProjectData = useCallback(async (selectedId) => {
      if (!selectedId) {
        setProject(null);
        setConversations([]);
        setConversationId("");
        setMessages([]);
        setPlan(null);
        setSources([]);
        return;
      }
      const [projectData, conversationData, planData, sourceData] = await Promise.all([
        api("GET", `/projects/${selectedId}`),
        api("GET", `/projects/${selectedId}/conversations`),
        api("GET", `/projects/${selectedId}/plan`),
        api("GET", `/projects/${selectedId}/sources`),
      ]);
      const nextConversations = conversationData.conversations || [];
      setProject(projectData);
      setConversations(nextConversations);
      setPlan(planData);
      setSources(sourceData.sources || []);
      setConversationId((previous) =>
        nextConversations.some((item) => item.conversation_id === previous)
          ? previous
          : nextConversations[0]?.conversation_id || ""
      );
      setPlanForm({ goal: planData?.plan?.goal || projectData.goal || "", milestone: planData?.milestones?.[0]?.title || "" });
    }, []);

    const refreshMessages = useCallback(async (selectedProjectId, selectedConversationId) => {
      if (!selectedProjectId || !selectedConversationId) {
        setMessages([]);
        return;
      }
      const result = await api(
        "GET",
        `/projects/${selectedProjectId}/conversations/${selectedConversationId}/messages`
      );
      setMessages(result.messages || []);
    }, []);

    useEffect(() => {
      api("GET", "/me")
        .then(async (result) => {
          setUser(result);
          await refreshProjects();
        })
        .catch(() => setUser(null))
        .finally(() => setAuthBusy(false));
    }, [refreshProjects]);

    useEffect(() => {
      if (!user) return;
      setError("");
      refreshProjectData(projectId).catch((caught) => setError(errorText(caught)));
    }, [projectId, user, refreshProjectData]);

    useEffect(() => {
      if (!user || !projectId || !conversationId) return;
      refreshMessages(projectId, conversationId).catch((caught) => setError(errorText(caught)));
    }, [projectId, conversationId, user, refreshMessages]);

    useEffect(() => {
      if (!user || !projectId || !conversationId) return;
      const stored = window.localStorage.getItem(`study:last-run:${projectId}`);
      if (!stored) return;
      try {
        const pointer = JSON.parse(stored);
        if (pointer.conversationId !== conversationId || !pointer.runId) return;
        api("GET", `/projects/${projectId}/teaching-runs/${pointer.runId}`)
          .then(setActiveRun)
          .catch(() => window.localStorage.removeItem(`study:last-run:${projectId}`));
      } catch (caught) {
        window.localStorage.removeItem(`study:last-run:${projectId}`);
      }
    }, [projectId, conversationId, user]);

    async function exchangeInvite(event) {
      event.preventDefault();
      if (!authToken.trim()) return;
      setAuthBusy(true);
      setError("");
      try {
        await api("POST", "/auth/invitations/exchange", { token: authToken.trim() }, { idempotent: false });
        const me = await api("GET", "/me");
        setUser(me);
        setAuthToken("");
        await refreshProjects();
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setAuthBusy(false);
      }
    }

    async function logout() {
      try {
        await api("POST", "/auth/logout", {}, { idempotent: false });
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setUser(null);
        setProjects([]);
        setProjectId("");
        setConversationId("");
      }
    }

    async function createProject(event) {
      event.preventDefault();
      if (!newProject.name.trim()) return;
      setLoading(true);
      setError("");
      try {
        const created = await api("POST", "/projects", {
          name: newProject.name.trim(),
          goal: newProject.goal.trim(),
        });
        setNewProject({ name: "", goal: "" });
        setNotice("项目已创建");
        await refreshProjects();
        setProjectId(created.project_id);
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function createConversation(event) {
      event.preventDefault();
      if (!projectId) return;
      setLoading(true);
      try {
        const created = await api("POST", `/projects/${projectId}/conversations`, { title: newConversation.trim() });
        setNewConversation("");
        setNotice("会话已创建");
        await refreshProjectData(projectId);
        setConversationId(created.conversation_id);
        setView("conversation");
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function pollRun(statusUrl, runId) {
      for (let attempt = 0; attempt < 80; attempt += 1) {
        const current = await api("GET", statusUrl);
        setActiveRun(current);
        if (["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
          await refreshMessages(projectId, conversationId);
          if (current.status === "succeeded") setNotice("回答已准备好");
          return current;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      setError("运行仍在处理中，刷新页面即可继续查看");
      return null;
    }

    async function askQuestion(event) {
      event.preventDefault();
      if (!projectId || !conversationId || !question.trim()) return;
      setLoading(true);
      setError("");
      setNotice("");
      try {
        const created = await api(
          "POST",
          `/projects/${projectId}/conversations/${conversationId}/teaching-runs`,
          { question: question.trim() }
        );
        setQuestion("");
        window.localStorage.setItem(`study:last-run:${projectId}`, JSON.stringify({ runId: created.run_id, conversationId }));
        setActiveRun({ ...created, status: created.status || "queued" });
        await refreshMessages(projectId, conversationId);
        await pollRun(created.status_url, created.run_id);
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function savePlan(event) {
      event.preventDefault();
      if (!projectId || !planForm.goal.trim() || !planForm.milestone.trim()) return;
      setLoading(true);
      try {
        const saved = await api("PUT", `/projects/${projectId}/plan`, {
          goal: planForm.goal.trim(),
          milestones: [{ title: planForm.milestone.trim(), description: "", tasks: [] }],
        });
        setPlan(saved);
        setNotice("计划已保存");
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function saveSource(event) {
      event.preventDefault();
      if (!projectId || !sourceForm.displayName.trim() || !sourceForm.title.trim() || !sourceForm.content.trim()) return;
      setLoading(true);
      try {
        const source = await api("POST", `/projects/${projectId}/sources`, {
          display_name: sourceForm.displayName.trim(),
          media_type: sourceForm.mediaType,
          acquisition: { kind: "user_input", label: sourceForm.displayName.trim() },
        });
        await api("POST", `/projects/${projectId}/sources/${source.source_id}/content`, {
          title: sourceForm.title.trim(),
          content: sourceForm.content,
          media_type: sourceForm.mediaType,
          language: "zh",
        });
        setSourceForm({ displayName: "", title: "", content: "", mediaType: "text/markdown" });
        await refreshProjectData(projectId);
        setNotice("资料已登记，正在等待处理");
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function reloadWorkspace() {
      setLoading(true);
      setError("");
      try {
        await refreshProjects();
        await refreshProjectData(projectId);
        await refreshMessages(projectId, conversationId);
        setNotice("已刷新");
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    if (authBusy && !user) return h(LoadingScreen);
    if (!user) return h(AuthScreen, { token: authToken, setToken: setAuthToken, onSubmit: exchangeInvite, busy: authBusy, error });

    return h(
      "div",
      { className: "app-shell" },
      h(
        "header",
        { className: "topbar" },
        h("div", { className: "brand" }, h("span", { className: "brand-mark", "aria-hidden": true }, "学"), h("span", null, "学习工作台")),
        h("div", { className: "topbar-actions" },
          h("span", { className: "identity" }, user.display_name || user.principal_id),
          h("button", { className: "icon-button", onClick: reloadWorkspace, disabled: loading, title: "刷新工作区", "aria-label": "刷新工作区" }, "↻"),
          h("button", { className: "quiet-button", onClick: logout }, "退出")
        )
      ),
      h(
        "div",
        { className: "workspace" },
        h(ProjectRail, {
          projects, projectId, setProjectId, newProject, setNewProject, createProject, loading,
        }),
        h("main", { className: "main-column" },
          error && h("div", { className: "alert alert-error", role: "alert" }, h("span", null, error), h("button", { onClick: () => setError("") , "aria-label": "关闭错误" }, "×")),
          notice && h("div", { className: "alert alert-success", role: "status" }, notice),
          h("div", { className: "workspace-heading" },
            h("div", null, h("p", { className: "eyebrow" }, project ? "当前项目" : "学习空间"), h("h1", null, project ? project.name : "选择一个项目"), project && h("p", { className: "subheading" }, project.goal || "还没有写下项目目标")),
            project && h("form", { className: "new-conversation", onSubmit: createConversation }, h("input", { value: newConversation, onChange: (event) => setNewConversation(event.target.value), placeholder: "新会话名称", "aria-label": "新会话名称" }), h("button", { className: "primary-button compact", disabled: loading }, "+ 新会话"))
          ),
          project && h(WorkspaceTabs, { view, setView }),
          project ? h(MainView, { view, projectId, conversations, conversationId, setConversationId, currentConversation, messages, question, setQuestion, askQuestion, loading, activeRun, plan, planForm, setPlanForm, savePlan, sources, sourceForm, setSourceForm, saveSource }) : h(EmptyProject, { onFocus: () => document.querySelector(".project-form input")?.focus() })
        ),
        project && h(EvidenceRail, { activeRun, sources, projectId, plan, view })
      )
    );
  }

  function LoadingScreen() {
    return h("div", { className: "center-screen" }, h("div", { className: "loading-mark", "aria-label": "正在加载" }, "学"), h("p", null, "正在打开学习空间"));
  }

  function AuthScreen({ token, setToken, onSubmit, busy, error }) {
    return h("main", { className: "auth-screen" }, h("section", { className: "auth-panel" },
      h("div", { className: "brand large" }, h("span", { className: "brand-mark", "aria-hidden": true }, "学"), h("span", null, "学习工作台")),
      h("h1", null, "把下一步学什么，变成今天能完成的事"),
      h("p", { className: "auth-copy" }, "用邀请访问码进入你的学习空间。"),
      error && h("div", { className: "alert alert-error", role: "alert" }, error),
      h("form", { onSubmit, className: "auth-form" },
        h("label", { htmlFor: "invite-token" }, "邀请访问码"),
        h("input", { id: "invite-token", value: token, onChange: (event) => setToken(event.target.value), autoComplete: "one-time-code", placeholder: "粘贴访问码", disabled: busy }),
        h("button", { className: "primary-button", disabled: busy || !token.trim() }, busy ? "正在进入…" : "进入学习空间")
      )
    ));
  }

  function ProjectRail({ projects, projectId, setProjectId, newProject, setNewProject, createProject, loading }) {
    return h("aside", { className: "project-rail" },
      h("div", { className: "rail-heading" }, h("span", null, "项目"), h("span", { className: "count" }, projects.length)),
      h("nav", { className: "project-list", "aria-label": "学习项目" }, projects.length ? projects.map((item) => h("button", { key: item.project_id, className: `project-item ${item.project_id === projectId ? "selected" : ""}`, onClick: () => setProjectId(item.project_id) }, h("span", { className: "project-dot", "aria-hidden": true }, ""), h("span", null, item.name))) : h("p", { className: "rail-empty" }, "还没有项目")),
      h("form", { className: "project-form", onSubmit: createProject },
        h("label", { htmlFor: "project-name" }, "新建项目"),
        h("input", { id: "project-name", value: newProject.name, onChange: (event) => setNewProject({ ...newProject, name: event.target.value }), placeholder: "例如：Agent 工程基础" }),
        h("textarea", { value: newProject.goal, onChange: (event) => setNewProject({ ...newProject, goal: event.target.value }), placeholder: "项目目标（可选）", rows: 3 }),
        h("button", { className: "primary-button compact", disabled: loading || !newProject.name.trim() }, "+ 创建项目")
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
      h("form", { className: "plan-form", onSubmit: savePlan },
        h("label", { htmlFor: "plan-goal" }, "学习目标"),
        h("textarea", { id: "plan-goal", value: planForm.goal, onChange: (event) => setPlanForm({ ...planForm, goal: event.target.value }), rows: 4, placeholder: "完成后你希望能独立做什么？" }),
        h("label", { htmlFor: "plan-milestone" }, "第一个里程碑"),
        h("input", { id: "plan-milestone", value: planForm.milestone, onChange: (event) => setPlanForm({ ...planForm, milestone: event.target.value }), placeholder: "例如：能解释 Agent harness 的关键边界" }),
        h("button", { className: "primary-button", disabled: loading || !planForm.goal.trim() || !planForm.milestone.trim() }, loading ? "保存中…" : "保存计划")
      ),
      plan && h("div", { className: "plan-summary" },
        milestones.map((milestone) => h("div", { className: "milestone-row", key: milestone.milestone_id }, h(StatusDot, { status: "queued" }), h("span", null, milestone.title))),
        tasks.map((task) => h("div", { className: "task-row", key: task.task_id }, h("span", { className: "task-check", "aria-hidden": true }, "□"), h("span", null, task.title)))
      )
    );
  }

  function SourcesView({ sources, sourceForm, setSourceForm, saveSource, loading }) {
    return h("section", { className: "content-section form-section" }, h("div", { className: "section-title" }, h("div", null, h("p", { className: "eyebrow" }, "项目资料"), h("h2", null, `${sources.length} 份资料`))), h("form", { className: "source-form", onSubmit: saveSource }, h("label", { htmlFor: "source-display" }, "资料名称"), h("input", { id: "source-display", value: sourceForm.displayName, onChange: (event) => setSourceForm({ ...sourceForm, displayName: event.target.value }), placeholder: "例如：Agent 设计笔记" }), h("label", { htmlFor: "source-title" }, "文档标题"), h("input", { id: "source-title", value: sourceForm.title, onChange: (event) => setSourceForm({ ...sourceForm, title: event.target.value }), placeholder: "例如：工具调用边界" }), h("label", { htmlFor: "source-content" }, "正文"), h("textarea", { id: "source-content", value: sourceForm.content, onChange: (event) => setSourceForm({ ...sourceForm, content: event.target.value }), rows: 8, placeholder: "粘贴 Markdown 或纯文本" }), h("button", { className: "primary-button", disabled: loading || !sourceForm.content.trim() }, loading ? "登记中…" : "登记资料")), h("div", { className: "source-list" }, sources.length ? sources.map((source) => h("div", { className: "source-row", key: source.source_id }, h("div", null, h("strong", null, source.display_name), h("span", null, source.media_type || "未标注类型")), h("span", { className: "source-status" }, "已登记"))) : h("div", { className: "empty-state compact-empty" }, h("h2", null, "还没有资料"), h("p", null, "登记资料后，回答可以带上原文引用。"))));
  }

  function EvidenceRail({ activeRun, sources, plan, view }) {
    const statusLabel = activeRun?.status === "succeeded"
      ? "回答已核验"
      : activeRun?.status === "reconciliation_required"
        ? "需要对账"
        : "运行状态";
    const evidence = activeRun
      ? h(React.Fragment, null,
        h("div", { className: "evidence-status" }, h(StatusDot, { status: activeRun.status }), h("strong", null, statusLabel)),
        activeRun.grounding && h("p", { className: "grounding" }, activeRun.grounding === "sourced" ? "回答含有已核验来源" : "回答没有通过核验的来源"),
        activeRun.citations?.length
          ? h("div", { className: "citation-list" }, activeRun.citations.map((citation, index) => h("div", { className: "citation", key: `${citation.source_id}-${index}` },
            h("span", { className: "citation-index" }, String(index + 1).padStart(2, "0")),
            h("div", null, h("strong", null, citation.source_id), h("span", null, `第 ${citation.span_start}–${citation.span_end} 字`), h("code", null, citation.content_hash))
          )))
          : h("div", { className: "empty-rail" }, "完成回答后，已核验的引用会出现在这里。")
      )
      : h("div", { className: "empty-rail" }, sources.length ? `${sources.length} 份资料已登记，等待回答引用。` : plan ? "先登记资料，回答才有可回读的证据。" : "选择一个项目开始。");
    return h("aside", { className: "evidence-rail" }, h("div", { className: "rail-heading" }, h("span", null, view === "sources" ? "资料状态" : "证据")), evidence);
  }

  function StatusDot({ status }) {
    return h("span", { className: `status-dot ${status}`, "aria-hidden": true });
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();

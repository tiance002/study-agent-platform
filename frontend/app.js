/* global React, ReactDOM */

(function () {
  "use strict";

  const { useCallback, useEffect, useMemo, useRef, useState } = React;
  const h = React.createElement;

  class ApiError extends Error {
    constructor(status, payload) {
      super(payload && payload.message ? payload.message : "请求未完成");
      this.status = status;
      this.payload = payload || {};
    }
  }

  const writeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  const requestKey = (scope) => {
    const safeScope = String(scope).replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 80) || "web";
    return `${safeScope}-${crypto.randomUUID()}`;
  };
  const runStorageKey = (principalId, projectId, conversationId) =>
    `study:last-run:${principalId}:${projectId}:${conversationId}`;

  function savedSelection(principalId) {
    try {
      return JSON.parse(localStorage.getItem(`study:selection:${principalId}`)) || {};
    } catch (_) {
      return {};
    }
  }

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
      signal: settings.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (error) {
      payload = { message: text || "服务器返回了无法读取的响应" };
    }
    if (response.status === 401 && path !== "/auth/invitations/exchange") {
      window.dispatchEvent(new Event("study:session-expired"));
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
    if (code === "already_authenticated") return "当前浏览器已有会话，请先退出后再切换账号";
    if (code === "USERNAME_TAKEN") return "用户名已注册，请换一个用户名";
    if (code === "REGISTRATION_DISABLED") return "注册暂未开放";
    if (code === "PASSWORD_LOGIN_DISABLED") return "密码登录暂未启用";
    if (error.status === 401) return "用户名或密码错误，或会话已失效";
    return (error.payload && error.payload.message) || `请求失败（${error.status}）`;
  }

  function App() {
    const [user, setUser] = useState(null);
    const [authToken, setAuthToken] = useState("");
    // Open-registration deployments should lead with the account workflow;
    // invitation exchange remains available as a compatibility tab.
    const [authMode, setAuthMode] = useState("login");
    const [authCredentials, setAuthCredentials] = useState({ username: "", password: "" });
    const [authBusy, setAuthBusy] = useState(true);
    const [projects, setProjects] = useState([]);
    const [projectId, setProjectId] = useState("");
    const [project, setProject] = useState(null);
    const [conversations, setConversations] = useState([]);
    const [conversationId, setConversationId] = useState("");
    const [messages, setMessages] = useState([]);
    const [plan, setPlan] = useState(null);
    const [sources, setSources] = useState([]);
    const [ingestionJobs, setIngestionJobs] = useState([]);
    const [citationReading, setCitationReading] = useState(null);
    const [activeRun, setActiveRun] = useState(null);
    const [view, setView] = useState("conversation");
    const [notice, writeNotice] = useState("");
    const [error, writeError] = useState("");
    const [loading, writeLoading] = useState(false);
    const [pendingCommand, setPendingCommand] = useState(null);
    const scopeRef = useRef({ key: "", principalId: "", projectId: "", conversationId: "", epoch: 0 });
    const commandRef = useRef(new Map());
    const pollControllerRef = useRef(null);

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

    const principalId = user?.principal_id || "";
    const scopeKey = `${principalId}|${projectId}|${conversationId}`;
    if (scopeRef.current.key !== scopeKey) {
      scopeRef.current = {
        key: scopeKey,
        principalId,
        projectId,
        conversationId,
        epoch: scopeRef.current.epoch + 1,
      };
    }
    const renderEpoch = scopeRef.current.epoch;
    const setNotice = (value) => { if (scopeRef.current.epoch === renderEpoch) writeNotice(value); };
    const setError = (value) => { if (scopeRef.current.epoch === renderEpoch) writeError(value); };
    const setLoading = (value) => { if (scopeRef.current.epoch === renderEpoch) writeLoading(value); };

    function currentProject(scope) {
      return scopeRef.current.principalId === scope.principalId
        && scopeRef.current.projectId === scope.projectId
        && (scope.epoch === undefined || scopeRef.current.epoch === scope.epoch);
    }

    function currentView(scope) {
      return currentProject(scope) && scopeRef.current.conversationId === scope.conversationId;
    }

    async function executeCommand(name, label, method, path, body, onSuccess) {
      const scope = { ...scopeRef.current };
      const bodyText = JSON.stringify(body);
      let command = commandRef.current.get(name);
      const unresolved = [...commandRef.current.values()].find((item) => item.state === "unknown");
      if (unresolved && (unresolved !== command || unresolved.bodyText !== bodyText)) {
        throw new ApiError(409, { message: "请先重试结果未知的请求，确认完成后再提交新内容。" });
      }
      if (command?.state === "submitting") return;
      if (!command || command.path !== path || command.bodyText !== bodyText) {
        command = { name, label, method, path, body: JSON.parse(bodyText), bodyText, key: requestKey(name), onSuccess, scope };
        commandRef.current.set(name, command);
      }
      command.state = "submitting";
      let result;
      try {
        result = await api(method, path, command.body, { key: command.key });
      } catch (caught) {
        if (!(caught instanceof ApiError)) {
          command.state = "unknown";
          if (currentView(scope)) setPendingCommand({ ...command, retrying: false });
        } else {
          commandRef.current.delete(name);
        }
        throw caught;
      }
      commandRef.current.delete(name);
      if (!currentView(scope)) return;
      setPendingCommand(null);
      return await onSuccess(result);
    }

    const currentConversation = useMemo(
      () => conversations.find((item) => item.conversation_id === conversationId) || null,
      [conversations, conversationId]
    );

    const refreshProjects = useCallback(async (owner = scopeRef.current.principalId) => {
      const epoch = scopeRef.current.epoch;
      const result = await api("GET", "/projects");
      if (scopeRef.current.principalId !== owner && scopeRef.current.epoch !== epoch) return [];
      const next = result.projects || [];
      setProjects(next);
      setProjectId((previous) => {
        const candidate = previous || savedSelection(owner).projectId;
        return next.some((item) => item.project_id === candidate) ? candidate : next[0]?.project_id || "";
      });
      return next;
    }, []);

    const refreshProjectData = useCallback(async (selectedId, signal) => {
      if (!selectedId) {
        setProject(null);
        setConversations([]);
        setConversationId("");
        setMessages([]);
        setPlan(null);
        setSources([]);
        setIngestionJobs([]);
        return;
      }
      const scope = { principalId, projectId: selectedId, epoch: scopeRef.current.epoch };
      const [projectData, conversationData, planData, sourceData, jobData] = await Promise.all([
        api("GET", `/projects/${selectedId}`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/conversations`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/plan`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/sources`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/ingestion-jobs`, undefined, { signal }),
      ]);
      if (!currentProject(scope)) return;
      const nextConversations = conversationData.conversations || [];
      setProject(projectData);
      setConversations(nextConversations);
      setPlan(planData);
      setSources(sourceData.sources || []);
      setIngestionJobs(jobData.jobs || []);
      setConversationId((previous) =>
        nextConversations.some((item) => item.conversation_id === previous)
          ? previous
          : nextConversations.find((item) => item.conversation_id === savedSelection(principalId).conversationId)?.conversation_id || nextConversations[0]?.conversation_id || ""
      );
      setPlanForm({ goal: planData?.plan?.goal || projectData.goal || "", milestone: planData?.milestones?.[0]?.title || "" });
    }, [principalId]);

    const refreshMessages = useCallback(async (selectedProjectId, selectedConversationId, signal) => {
      if (!selectedProjectId || !selectedConversationId) {
        setMessages([]);
        return;
      }
      const scope = { principalId, projectId: selectedProjectId, conversationId: selectedConversationId, epoch: scopeRef.current.epoch };
      const result = await api(
        "GET",
        `/projects/${selectedProjectId}/conversations/${selectedConversationId}/messages`,
        undefined,
        { signal }
      );
      if (!currentView(scope)) return;
      setMessages(result.messages || []);
    }, [principalId]);

    useEffect(() => {
      api("GET", "/me")
        .then(async (result) => {
          setUser(result);
          await refreshProjects(result.principal_id);
        })
        .catch(() => setUser(null))
        .finally(() => setAuthBusy(false));
    }, [refreshProjects]);

    useEffect(() => {
      if (pollControllerRef.current) {
        pollControllerRef.current.abort();
        pollControllerRef.current = null;
      }
      setProject(null);
      setConversations([]);
      setMessages([]);
      setPlan(null);
      setSources([]);
      setIngestionJobs([]);
      setActiveRun(null);
      setCitationReading(null);
      setQuestion("");
      setNewConversation("");
      setSourceForm({ displayName: "", title: "", content: "", mediaType: "text/markdown" });
      setPlanForm({ goal: "", milestone: "" });
    }, [principalId, projectId]);

    useEffect(() => {
      if (pollControllerRef.current) {
        pollControllerRef.current.abort();
        pollControllerRef.current = null;
      }
      setMessages([]);
      setActiveRun(null);
      setCitationReading(null);
      writeLoading(false);
      writeError("");
      writeNotice("");
      setQuestion("");
      setPendingCommand(null);
    }, [principalId, projectId, conversationId]);

    useEffect(() => {
      if (principalId && projectId && conversationId) {
        localStorage.setItem(`study:selection:${principalId}`, JSON.stringify({ projectId, conversationId }));
      }
    }, [principalId, projectId, conversationId]);

    useEffect(() => {
      const expired = () => {
        setUser(null);
        setProjects([]);
        setProjectId("");
        setConversationId("");
        setNewProject({ name: "", goal: "" });
        commandRef.current.clear();
        writeLoading(false);
      };
      window.addEventListener("study:session-expired", expired);
      return () => window.removeEventListener("study:session-expired", expired);
    }, []);

    useEffect(() => {
      if (!user) return;
      const controller = new AbortController();
      setError("");
      refreshProjectData(projectId, controller.signal).catch((caught) => {
        if (caught.name !== "AbortError") setError(errorText(caught));
      });
      return () => controller.abort();
    }, [projectId, principalId, user, refreshProjectData]);

    useEffect(() => {
      if (!user || !projectId || !conversationId) return;
      const controller = new AbortController();
      refreshMessages(projectId, conversationId, controller.signal).catch((caught) => {
        if (caught.name !== "AbortError") setError(errorText(caught));
      });
      return () => controller.abort();
    }, [projectId, conversationId, principalId, user, refreshMessages]);

    useEffect(() => {
      if (!user || !projectId || !conversationId) return;
      const controller = new AbortController();
      const scope = { principalId, projectId, conversationId, epoch: scopeRef.current.epoch };
      const storageKey = runStorageKey(principalId, projectId, conversationId);
      const stored = window.localStorage.getItem(storageKey);
      if (!stored) return;
      try {
        const pointer = JSON.parse(stored);
        if (pointer.projectId !== projectId || pointer.conversationId !== conversationId || !pointer.runId) return;
        api("GET", `/projects/${projectId}/teaching-runs/${pointer.runId}`, undefined, { signal: controller.signal })
          .then(async (current) => {
            if (!currentView(scope)) return;
            setActiveRun(current);
            if (!["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
              await pollRun(
                current.status_url || `/projects/${projectId}/teaching-runs/${pointer.runId}`,
                pointer.runId,
                projectId,
                conversationId,
                scope,
                controller.signal
              );
            }
          })
          .catch((caught) => {
            if (caught.name === "AbortError") return;
            if (caught instanceof ApiError && caught.status === 404) window.localStorage.removeItem(storageKey);
            else setError(errorText(caught));
          });
      } catch (caught) {
        window.localStorage.removeItem(storageKey);
      }
      return () => controller.abort();
    }, [projectId, conversationId, principalId, user]);

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
        await refreshProjects(me.principal_id);
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setAuthBusy(false);
      }
    }

    async function passwordAuth(event) {
      event.preventDefault();
      if (!authCredentials.username.trim() || !authCredentials.password) return;
      setAuthBusy(true);
      setError("");
      const path = authMode === "register" ? "/auth/register" : "/auth/login";
      try {
        const result = await api("POST", path, authCredentials, { idempotent: false });
        setAuthCredentials({ username: "", password: "" });
        const me = await api("GET", "/me");
        setUser(me);
        await refreshProjects(me.principal_id);
        if (authMode === "register" && result?.default_project_id) setProjectId(result.default_project_id);
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
        setProject(null);
        setConversations([]);
        setMessages([]);
        setPlan(null);
        setSources([]);
        setIngestionJobs([]);
        setActiveRun(null);
        setNotice("");
        setCitationReading(null);
        setPendingCommand(null);
        commandRef.current.clear();
        setNewProject({ name: "", goal: "" });
      }
    }

    async function retryPendingCommand() {
      if (!pendingCommand || pendingCommand.retrying) return;
      if (!currentView(pendingCommand.scope)) return;
      setPendingCommand({ ...pendingCommand, retrying: true });
      setLoading(true);
      setError("");
      try {
        await executeCommand(
          pendingCommand.name,
          pendingCommand.label,
          pendingCommand.method,
          pendingCommand.path,
          pendingCommand.body,
          pendingCommand.onSuccess
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function createProject(event) {
      event.preventDefault();
      if (!newProject.name.trim()) return;
      setLoading(true);
      setError("");
      try {
        await executeCommand("create-project", "创建项目", "POST", "/projects", {
          name: newProject.name.trim(),
          goal: newProject.goal.trim(),
        }, async (created) => {
          setNewProject({ name: "", goal: "" });
          setNotice("项目已创建");
          await refreshProjects();
          if (scopeRef.current.epoch !== renderEpoch) return;
          setConversationId("");
          setProjectId(created.project_id);
        });
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
        await executeCommand(
          `create-conversation:${projectId}`,
          "创建会话",
          "POST",
          `/projects/${projectId}/conversations`,
          { title: newConversation.trim() },
          async (created) => {
            setNewConversation("");
            setNotice("会话已创建");
            await refreshProjectData(projectId);
            if (scopeRef.current.epoch !== renderEpoch) return;
            setConversationId(created.conversation_id);
            setView("conversation");
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function pollRun(statusUrl, runId, selectedProjectId, selectedConversationId, scope, signal) {
      for (let attempt = 0; attempt < 80; attempt += 1) {
        let current;
        try {
          current = await api("GET", statusUrl, undefined, { signal });
        } catch (caught) {
          if (signal?.aborted || !currentView(scope)) return null;
          if (caught instanceof ApiError && caught.status < 500) throw caught;
          setError("连接暂时中断，正在继续查询回答进度");
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        if (!currentView(scope)) return null;
        setError("");
        setActiveRun(current);
        if (["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
          await refreshMessages(selectedProjectId, selectedConversationId, signal);
          if (currentView(scope) && current.status === "succeeded") setNotice("回答已准备好");
          return current;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      if (currentView(scope)) setError("运行仍在处理中，刷新工作区即可继续查看");
      return null;
    }

    async function askQuestion(event) {
      event.preventDefault();
      if (!projectId || !conversationId || !question.trim() || pendingCommand) return;
      setLoading(true);
      setError("");
      setNotice("");
      try {
        const selectedProjectId = projectId;
        const selectedConversationId = conversationId;
        const scope = { principalId, projectId: selectedProjectId, conversationId: selectedConversationId, epoch: scopeRef.current.epoch };
        await executeCommand(
          `teaching-run:${selectedProjectId}:${selectedConversationId}`,
          "发送问题",
          "POST",
          `/projects/${selectedProjectId}/conversations/${selectedConversationId}/teaching-runs`,
          { question: question.trim() },
          async (created) => {
            setQuestion("");
            window.localStorage.setItem(
              runStorageKey(principalId, selectedProjectId, selectedConversationId),
              JSON.stringify({ runId: created.run_id, projectId: selectedProjectId, conversationId: selectedConversationId })
            );
            if (!currentView(scope)) return;
            setActiveRun({ ...created, status: created.status || "queued" });
            await refreshMessages(selectedProjectId, selectedConversationId);
            const controller = new AbortController();
            pollControllerRef.current = controller;
            try {
              await pollRun(
                created.status_url,
                created.run_id,
                selectedProjectId,
                selectedConversationId,
                scope,
                controller.signal
              );
            } finally {
              if (pollControllerRef.current === controller) pollControllerRef.current = null;
            }
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function savePlan(event) {
      event.preventDefault();
      if (plan || !projectId || !planForm.goal.trim() || !planForm.milestone.trim()) return;
      setLoading(true);
      try {
        await executeCommand("create-plan", "创建计划", "PUT", `/projects/${projectId}/plan`, {
          goal: planForm.goal.trim(),
          milestones: [{ title: planForm.milestone.trim(), description: "", tasks: [] }],
        }, async (saved) => {
          setPlan(saved);
          setNotice("计划已创建");
        });
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function pollIngestionJob(selectedProjectId, jobId) {
      const scope = { principalId, projectId: selectedProjectId, epoch: scopeRef.current.epoch };
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const current = await api("GET", `/projects/${selectedProjectId}/ingestion-jobs/${jobId}`);
        if (!currentProject(scope)) return;
        setIngestionJobs((previous) => [current, ...previous.filter((item) => item.job_id !== jobId)]);
        if (["succeeded", "failed"].includes(current.status)) return current;
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
      return null;
    }

    async function readCitation(citation) {
      if (!projectId || !citation?.document_id) return;
      const scope = {
        principalId,
        projectId,
        conversationId,
        epoch: scopeRef.current.epoch,
      };
      const key = `${citation.document_id}:${citation.span_start}:${citation.span_end}`;
      setCitationReading({ key, loading: true });
      const query = new URLSearchParams({
        document_id: citation.document_id,
        start: String(citation.span_start),
        end: String(citation.span_end),
        content_hash: citation.content_hash || "",
      });
      try {
        const result = await api("GET", `/projects/${projectId}/sources/${citation.source_id}/span?${query.toString()}`);
        if (currentView(scope)) setCitationReading({ key, loading: false, result });
      } catch (caught) {
        if (currentView(scope)) setCitationReading({ key, loading: false, error: errorText(caught) });
      }
    }

    async function saveSource(event) {
      event.preventDefault();
      if (!projectId || !sourceForm.displayName.trim() || !sourceForm.title.trim() || !sourceForm.content.trim()) return;
      setLoading(true);
      try {
        await executeCommand(
          `register-source:${projectId}:${sourceForm.displayName.trim()}`,
          "登记资料",
          "POST",
          `/projects/${projectId}/sources`,
          {
          display_name: sourceForm.displayName.trim(),
          media_type: sourceForm.mediaType,
          acquisition: { kind: "user_input", label: sourceForm.displayName.trim() },
          },
          async (source) => {
            const contentBody = {
              title: sourceForm.title.trim(),
              content: sourceForm.content,
              media_type: sourceForm.mediaType,
              language: "zh",
            };
            await executeCommand(
              `upload-source:${projectId}:${source.source_id}`,
              "上传资料正文",
              "POST",
              `/projects/${projectId}/sources/${source.source_id}/content`,
              contentBody,
              async (uploaded) => {
                setIngestionJobs((previous) => [uploaded.job, ...previous.filter((item) => item.job_id !== uploaded.job.job_id)]);
                setSourceForm({ displayName: "", title: "", content: "", mediaType: "text/markdown" });
                await refreshProjectData(projectId);
                setNotice("资料已登记，正在处理");
                pollIngestionJob(projectId, uploaded.job.job_id).catch((caught) => setError(errorText(caught)));
              }
            );
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function reloadWorkspace() {
      const scope = { ...scopeRef.current };
      setLoading(true);
      setError("");
      try {
        await refreshProjects();
        await refreshProjectData(projectId);
        await refreshMessages(projectId, conversationId);
        const stored = window.localStorage.getItem(runStorageKey(principalId, projectId, conversationId));
        if (stored) {
          const pointer = JSON.parse(stored);
          const current = await api("GET", `/projects/${projectId}/teaching-runs/${pointer.runId}`);
          if (currentView(scope)) {
            setActiveRun(current);
            if (!["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
              await pollRun(`/projects/${projectId}/teaching-runs/${pointer.runId}`, pointer.runId, projectId, conversationId, scope);
            }
          }
        }
        setNotice("已刷新");
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    if (authBusy && !user) return h(LoadingScreen);
    if (!user) return h(AuthScreen, {
      mode: authMode,
      setMode: setAuthMode,
      token: authToken,
      setToken: setAuthToken,
      credentials: authCredentials,
      setCredentials: setAuthCredentials,
      onInvite: exchangeInvite,
      onPassword: passwordAuth,
      busy: authBusy,
      error,
    });

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
          projects, projectId, setProjectId: (id) => { setConversationId(""); setProjectId(id); }, newProject, setNewProject, createProject, loading,
        }),
        h("main", { className: "main-column" },
          error && h("div", { className: "alert alert-error", role: "alert" }, h("span", null, error), h("button", { onClick: () => setError("") , "aria-label": "关闭错误" }, "×")),
          pendingCommand && h("div", { className: "alert alert-warning", role: "status" }, h("span", null, `${pendingCommand.label}结果未知，请确认后重试。`), h("button", { className: "secondary-button compact", onClick: retryPendingCommand, disabled: loading }, pendingCommand.retrying ? "重试中…" : "用同一请求重试")),
          notice && h("div", { className: "alert alert-success", role: "status" }, notice),
          h("div", { className: "workspace-heading" },
            h("div", null, h("p", { className: "eyebrow" }, project ? "当前项目" : "学习空间"), h("h1", null, project ? project.name : "选择一个项目"), project && h("p", { className: "subheading" }, project.goal || "还没有写下项目目标")),
            project && h("form", { className: "new-conversation", onSubmit: createConversation }, h("input", { value: newConversation, onChange: (event) => setNewConversation(event.target.value), placeholder: "新会话名称", "aria-label": "新会话名称" }), h("button", { className: "primary-button compact", disabled: loading }, "+ 新会话"))
          ),
          project && h(WorkspaceTabs, { view, setView }),
          project ? h(MainView, { view, projectId, conversations, conversationId, setConversationId, currentConversation, messages, question, setQuestion, askQuestion, loading, activeRun, plan, planForm, setPlanForm, savePlan, sources, ingestionJobs, sourceForm, setSourceForm, saveSource }) : h(EmptyProject, { onFocus: () => document.querySelector(".project-form input")?.focus() })
        ),
        project && h(EvidenceRail, { activeRun, sources, projectId, plan, view, onReadCitation: readCitation, citationReading })
      )
    );
  }

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

  function SourcesView({ sources, ingestionJobs, sourceForm, setSourceForm, saveSource, loading }) {
    const labels = { queued: "排队中", processing: "处理中", succeeded: "已处理", failed: "处理失败" };
    const latestJobs = new Map();
    (ingestionJobs || []).forEach((job) => {
      if (!latestJobs.has(job.source_id)) latestJobs.set(job.source_id, job);
    });
    return h("section", { className: "content-section form-section" }, h("div", { className: "section-title" }, h("div", null, h("p", { className: "eyebrow" }, "项目资料"), h("h2", null, `${sources.length} 份资料`))), h("form", { className: "source-form", onSubmit: saveSource }, h("label", { htmlFor: "source-display" }, "资料名称"), h("input", { id: "source-display", value: sourceForm.displayName, onChange: (event) => setSourceForm({ ...sourceForm, displayName: event.target.value }), placeholder: "例如：Agent 设计笔记" }), h("label", { htmlFor: "source-title" }, "文档标题"), h("input", { id: "source-title", value: sourceForm.title, onChange: (event) => setSourceForm({ ...sourceForm, title: event.target.value }), placeholder: "例如：工具调用边界" }), h("label", { htmlFor: "source-content" }, "正文"), h("textarea", { id: "source-content", value: sourceForm.content, onChange: (event) => setSourceForm({ ...sourceForm, content: event.target.value }), rows: 8, placeholder: "粘贴 Markdown 或纯文本" }), h("button", { className: "primary-button", disabled: loading || !sourceForm.content.trim() }, loading ? "登记中…" : "登记资料")), h("div", { className: "source-list" }, sources.length ? sources.map((source) => {
      const job = latestJobs.get(source.source_id);
      const status = job ? labels[job.status] || job.status : "已登记";
      return h("div", { className: "source-row", key: source.source_id }, h("div", null, h("strong", null, source.display_name), h("span", null, source.media_type || "未标注类型"), job && h("span", null, `版本 ${job.document_id}`)), h("span", { className: `source-status ${job?.status || "registered"}` }, status));
    }) : h("div", { className: "empty-state compact-empty" }, h("h2", null, "还没有资料"), h("p", null, "登记资料后，回答可以带上原文引用。"))));
  }

  function EvidenceRail({ activeRun, sources, plan, view, onReadCitation, citationReading }) {
    const statusLabel = activeRun?.status === "succeeded"
      ? activeRun.grounding === "sourced" ? "来源已核验" : "一般性回答（未核验来源）"
      : activeRun?.status === "reconciliation_required"
        ? "需要对账"
        : "运行状态";
    const evidence = activeRun
      ? h(React.Fragment, null,
        h("div", { className: "evidence-status" }, h(StatusDot, { status: activeRun.status }), h("strong", null, statusLabel)),
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

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();

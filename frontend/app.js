/* global React, ReactDOM */

(function () {
  "use strict";

  const { useEffect, useRef, useState } = React;
  const h = React.createElement;
  const { LoadingScreen, AuthScreen, ProjectRail, WorkspaceTabs, EmptyProject, MainView, EvidenceRail } = window.StudyViews;

  const { ApiError, runStorageKey, api } = window.StudyApi;
  const { useScopedCommands } = window.StudyCommands;
  const { useProjectState } = window.StudyProjectState;

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
    const [projectId, setProjectId] = useState("");
    const [projectFormOpen, setProjectFormOpen] = useState(false);
    const [conversationId, setConversationId] = useState("");
    const [citationReading, setCitationReading] = useState(null);
    const [activeRun, setActiveRun] = useState(null);
    const [view, setView] = useState("conversation");
    const [notice, writeNotice] = useState("");
    const [error, writeError] = useState("");
    const [loading, writeLoading] = useState(false);
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
    const [candidateForm, setCandidateForm] = useState({ url: "", title: "", snippet: "" });
    const [sourceSearchQuery, setSourceSearchQuery] = useState("");

    const principalId = user?.principal_id || "";

    const {
      pendingCommand,
      executeCommand,
      retryPendingCommand,
      dropCommand,
      clearPendingCommand,
      clearCommandRegistry,
      resetCommands,
      scopeRef,
      isProjectCurrent,
      isViewCurrent,
      setNotice,
      setError,
      setLoading,
    } = useScopedCommands({
      principalId,
      projectId,
      conversationId,
      onNotice: writeNotice,
      onError: writeError,
      onLoading: writeLoading,
      errorText,
    });

    const {
      projects,
      setProjects,
      project,
      setProject,
      conversations,
      setConversations,
      messages,
      setMessages,
      plan,
      setPlan,
      sources,
      setSources,
      ingestionJobs,
      setIngestionJobs,
      candidates,
      setCandidates,
      acquisitionJobs,
      setAcquisitionJobs,
      currentConversation,
      refreshProjects,
      refreshProjectData,
      refreshMessages,
      clearProjectData,
    } = useProjectState({
      principalId,
      conversationId,
      setProjectId,
      setConversationId,
      isProjectCurrent,
      isViewCurrent,
      scopeRef,
      onProjectDataLoaded: (projectData, planData) => setPlanForm({
        goal: planData?.plan?.goal || projectData.goal || "",
        milestone: planData?.milestones?.[0]?.title || "",
      }),
    });

    const renderEpoch = scopeRef.current.epoch;

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
      clearProjectData();
      setActiveRun(null);
      setCitationReading(null);
      setQuestion("");
      setNewConversation("");
      setSourceForm({ displayName: "", title: "", content: "", mediaType: "text/markdown" });
      setCandidateForm({ url: "", title: "", snippet: "" });
      setSourceSearchQuery("");
      setPlanForm({ goal: "", milestone: "" });
    }, [principalId, projectId, clearProjectData]);

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
      clearPendingCommand();
    }, [principalId, projectId, conversationId, setMessages, clearPendingCommand]);

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
        clearCommandRegistry();
        writeLoading(false);
      };
      window.addEventListener("study:session-expired", expired);
      return () => window.removeEventListener("study:session-expired", expired);
    }, [setProjects, clearCommandRegistry]);

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
            if (!isViewCurrent(scope)) return;
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
        resetCommands();
        setNewProject({ name: "", goal: "" });
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
          setProjectFormOpen(false);
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
          if (signal?.aborted || !isViewCurrent(scope)) return null;
          if (caught instanceof ApiError && caught.status < 500) throw caught;
          setError("连接暂时中断，正在继续查询回答进度");
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        if (!isViewCurrent(scope)) return null;
        setError("");
        setActiveRun(current);
        if (["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
          await refreshMessages(selectedProjectId, selectedConversationId, signal);
          if (isViewCurrent(scope) && current.status === "succeeded") setNotice("回答已准备好");
          return current;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      if (isViewCurrent(scope)) setError("运行仍在处理中，刷新工作区即可继续查看");
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
            if (!isViewCurrent(scope)) return;
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
        if (!isProjectCurrent(scope)) return;
        setIngestionJobs((previous) => [current, ...previous.filter((item) => item.job_id !== jobId)]);
        if (["succeeded", "failed"].includes(current.status)) return current;
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
      return null;
    }

    async function pollAcquisitionJob(selectedProjectId, acquisitionId) {
      const scope = { principalId, projectId: selectedProjectId, epoch: scopeRef.current.epoch };
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const current = await api("GET", `/projects/${selectedProjectId}/acquisition-jobs/${acquisitionId}`);
        if (!isProjectCurrent(scope)) return;
        setAcquisitionJobs((previous) => [current, ...previous.filter((item) => item.acquisition_id !== acquisitionId)]);
        if (["succeeded", "failed", "unknown"].includes(current.status)) return current;
        await new Promise((resolve) => setTimeout(resolve, 1000));
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
        if (isViewCurrent(scope)) setCitationReading({ key, loading: false, result });
      } catch (caught) {
        if (isViewCurrent(scope)) setCitationReading({ key, loading: false, error: errorText(caught) });
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

    async function saveCandidate(event) {
      event.preventDefault();
      if (!projectId || !candidateForm.url.trim() || !candidateForm.title.trim()) return;
      setLoading(true);
      try {
        await executeCommand(
          `create-candidate:${projectId}:${candidateForm.url.trim()}`,
          "登记候选资料",
          "POST",
          `/projects/${projectId}/source-candidates`,
          {
            url: candidateForm.url.trim(),
            title: candidateForm.title.trim(),
            snippet: candidateForm.snippet.trim(),
          },
          async (candidate) => {
            setCandidates((previous) => [candidate, ...previous.filter((item) => item.candidate_id !== candidate.candidate_id)]);
            setCandidateForm({ url: "", title: "", snippet: "" });
            setNotice("候选资料已登记，请确认后下载");
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function searchSourceCandidates(event) {
      event.preventDefault();
      const query = sourceSearchQuery.trim();
      if (!projectId || !query) return;
      setLoading(true);
      try {
        await executeCommand(
          `source-search:${projectId}:${query}`,
          "搜索资料",
          "POST",
          `/projects/${projectId}/source-search`,
          { query, limit: 8 },
          async (result) => {
            const found = result.candidates || [];
            setCandidates((previous) => [
              ...found,
              ...previous.filter((item) => !found.some((candidate) => candidate.candidate_id === item.candidate_id)),
            ]);
            setNotice(found.length ? `找到 ${found.length} 条候选资料，请确认后下载` : "没有找到可用资料候选");
          }
        );
      } catch (caught) {
        if (
          caught instanceof ApiError
          && ["SOURCE_SEARCH_DISABLED", "SOURCE_SEARCH_UNAVAILABLE"].includes(caught.payload?.code)
        ) {
          dropCommand(`source-search:${projectId}:${query}`);
        }
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function selectCandidate(candidate) {
      if (!projectId || candidate.status !== "discovered") return;
      setLoading(true);
      try {
        await executeCommand(
          `select-candidate:${projectId}:${candidate.candidate_id}`,
          "开始下载资料",
          "POST",
          `/projects/${projectId}/source-candidates/${candidate.candidate_id}/select`,
          { display_name: candidate.title, media_type: "text/markdown", language: "zh" },
          async (result) => {
            setAcquisitionJobs((previous) => [result.acquisition, ...previous.filter((item) => item.acquisition_id !== result.acquisition.acquisition_id)]);
            await refreshProjectData(projectId);
            setNotice("资料已加入下载队列");
            pollAcquisitionJob(projectId, result.acquisition.acquisition_id).catch((caught) => setError(errorText(caught)));
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
          if (isViewCurrent(scope)) {
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
          projects, projectId, setProjectId: (id) => { setConversationId(""); setProjectId(id); }, newProject, setNewProject, createProject, loading, projectFormOpen, setProjectFormOpen,
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
          project ? h(MainView, { view, projectId, conversations, conversationId, setConversationId, currentConversation, messages, question, setQuestion, askQuestion, loading, activeRun, plan, planForm, setPlanForm, savePlan, sources, ingestionJobs, candidates, acquisitionJobs, sourceForm, setSourceForm, saveSource, candidateForm, setCandidateForm, saveCandidate, sourceSearchQuery, setSourceSearchQuery, searchSourceCandidates, selectCandidate }) : h(EmptyProject, { onFocus: () => document.querySelector(".project-create summary")?.click() })
        ),
        project && h(EvidenceRail, { activeRun, sources, projectId, plan, view, onReadCitation: readCitation, citationReading })
      )
    );
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();

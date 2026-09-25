/* global React, ReactDOM, antd */

(function () {
  "use strict";

  const { useEffect, useRef, useState } = React;
  const h = React.createElement;
  const { ConfigProvider } = antd;
  const t = window.StudyTerms;
  const {
    LoadingScreen, AuthScreen, ProjectRail, KnowledgeBasePanel, WorkspaceTabs,
    EmptyProject, MainView, EvidenceRail, SettingsDrawer,
  } = window.StudyViews;

  const { ApiError, runStorageKey, api } = window.StudyApi;
  const { useScopedCommands } = window.StudyCommands;
  const { useProjectState } = window.StudyProjectState;

  // 星空主题 token —— 与 app.css 顶部的 --star-* 变量必须保持一致。
  const starfieldTheme = {
    algorithm: antd.theme.darkAlgorithm,
    token: {
      colorPrimary: "#2ee6d6",
      colorInfo: "#2ee6d6",
      colorSuccess: "#34d399",
      colorWarning: "#fbbf24",
      colorError: "#fb7185",
      colorBgBase: "#05070f",
      colorBgContainer: "rgba(17, 24, 48, 0.72)",
      colorBgElevated: "rgba(20, 28, 56, 0.94)",
      colorBgLayout: "transparent",
      colorBorder: "rgba(122, 162, 255, 0.28)",
      colorBorderSecondary: "rgba(122, 162, 255, 0.16)",
      colorText: "#e7ecff",
      colorTextSecondary: "#aab7dd",
      colorTextTertiary: "#7f8cb6",
      borderRadius: 10,
      fontSize: 14,
      // 关闭 antd 全局动效：既贴合"星空静止"的 reduced-motion 取向，也避免
      // 按钮 loading 图标在离场动画里残留（它会污染按钮的可访问名）。
      motion: false,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
      wireframe: false,
    },
    components: {
      Button: { primaryShadow: "0 0 18px rgba(46, 230, 214, 0.28)" },
      Drawer: { colorBgElevated: "#0b1226" },
      Input: { colorBgContainer: "rgba(9, 14, 32, 0.72)" },
      Select: { colorBgContainer: "rgba(9, 14, 32, 0.72)" },
      Tag: { defaultBg: "rgba(122, 162, 255, 0.16)" },
      Divider: { colorSplit: "rgba(122, 162, 255, 0.18)" },
    },
  };

  // antd 默认会在两个中文字之间插入空格（"退出" → "退 出"），
  // 那会让可访问名与门禁断言的精确文案不一致；这里显式关闭。
  // 同时关掉点击波纹：它会插入一层绝对定位覆盖元素，既与"无多余动效"的
  // 星空主题不一致，也会让自动化点击的命中测试变得不确定。
  const withTheme = (node) => h(ConfigProvider, {
    theme: starfieldTheme,
    button: { autoInsertSpace: false },
    autoInsertSpaceInButton: false,
    wave: { disabled: true },
  }, node);

  function errorText(error) {
    if (!(error instanceof ApiError)) return t.errors.network;
    const code = error.payload && error.payload.code;
    if (code && t.errors.byCode[code]) return t.errors.byCode[code];
    if (error.status === 401) return t.errors.unauthorized;
    return (error.payload && error.payload.message) || t.errors.fallback(error.status);
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
    const [settingsOpen, setSettingsOpen] = useState(false);
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

    // 学习闭环：诊断 → 生成计划 → 任务流转 → 自报提交。
    const [diagnosisForm, setDiagnosisForm] = useState({ experience_level: "beginner", weekly_hours: 6, preferred_style: "practice" });
    // planGenerated 只在本会话显式生成过计划时置真：刷新后是否可交互由
    // 服务端返回的任务字段决定，不靠本地猜测。
    const [planGenerated, setPlanGenerated] = useState(false);
    const [taskStates, setTaskStates] = useState({});
    const [submissionsByTask, setSubmissionsByTask] = useState({});
    const [taskBusyId, setTaskBusyId] = useState("");

    // 知识库（用户级）
    const [libraryForm, setLibraryForm] = useState({ name: "", url: "", content: "" });
    const [libraryLoading, setLibraryLoading] = useState(false);
    const [libraryError, setLibraryError] = useState("");
    const [libraryBusy, setLibraryBusy] = useState(false);
    const [libraryBusyId, setLibraryBusyId] = useState("");

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
      diagnosis,
      setDiagnosis,
      sources,
      setSources,
      ingestionJobs,
      setIngestionJobs,
      candidates,
      setCandidates,
      acquisitionJobs,
      setAcquisitionJobs,
      librarySources,
      refreshLibrary,
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
      setPlanGenerated(false);
      setTaskStates({});
      setSubmissionsByTask({});
      setTaskBusyId("");
      setDiagnosisForm({ experience_level: "beginner", weekly_hours: 6, preferred_style: "practice" });
    }, [principalId, projectId, clearProjectData]);

    useEffect(() => {
      if (!principalId) return;
      // 冷启动/刷新时 /me 已能识别账号，但此刻 scope epoch 会随 principalId
      // 首次落地而变化；登录回调里那次 refreshProjects 因此会被 guard 丢弃。
      // 这里在 principalId **稳定后**再读一次项目列表，刷新/重登才能读回项目。
      refreshProjects(principalId).catch((caught) => setError(errorText(caught)));
    }, [principalId, refreshProjects]);

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
      if (!principalId) {
        setLibraryForm({ name: "", url: "", content: "" });
        return;
      }
      setLibraryError("");
      setLibraryLoading(true);
      refreshLibrary()
        .catch((caught) => setLibraryError(errorText(caught)))
        .finally(() => setLibraryLoading(false));
    }, [principalId, refreshLibrary]);

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
        setDiagnosis(null);
        setSources([]);
        setIngestionJobs([]);
        setActiveRun(null);
        setNotice("");
        setCitationReading(null);
        setSettingsOpen(false);
        setPlanGenerated(false);
        setTaskStates({});
        setSubmissionsByTask({});
        setLibraryForm({ name: "", url: "", content: "" });
        setLibraryError("");
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
        await executeCommand("create-project", t.commands.createProject, "POST", "/projects", {
          name: newProject.name.trim(),
          goal: newProject.goal.trim(),
        }, async (created) => {
          setNewProject({ name: "", goal: "" });
          setNotice(t.notices.projectCreated);
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
          t.commands.createConversation,
          "POST",
          `/projects/${projectId}/conversations`,
          { title: newConversation.trim() },
          async (created) => {
            setNewConversation("");
            setNotice(t.workspace.conversationCreated);
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
          setError(t.conversation.polling);
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        if (!isViewCurrent(scope)) return null;
        setError("");
        setActiveRun(current);
        if (["succeeded", "failed", "reconciliation_required"].includes(current.status)) {
          await refreshMessages(selectedProjectId, selectedConversationId, signal);
          if (isViewCurrent(scope) && current.status === "succeeded") setNotice(t.conversation.answerReady);
          return current;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      if (isViewCurrent(scope)) setError(t.conversation.pollTimeout);
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
          t.commands.sendQuestion,
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
        await executeCommand("create-plan", t.commands.createPlan, "PUT", `/projects/${projectId}/plan`, {
          goal: planForm.goal.trim(),
          milestones: [{ title: planForm.milestone.trim(), description: "", tasks: [] }],
        }, async (saved) => {
          setPlan(saved);
          setNotice(t.plan.create);
        });
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function saveDiagnosis(event) {
      event.preventDefault();
      if (!projectId) return;
      const selectedProjectId = projectId;
      setLoading(true);
      try {
        await executeCommand(
          `diagnosis:${selectedProjectId}`,
          t.commands.saveDiagnosis,
          "POST",
          `/projects/${selectedProjectId}/diagnosis`,
          {
            experience_level: diagnosisForm.experience_level,
            weekly_hours: diagnosisForm.weekly_hours,
            preferred_style: diagnosisForm.preferred_style,
          },
          async (saved) => {
            setDiagnosis(saved);
            setNotice(t.diagnosis.saved);
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function generatePlan() {
      if (!projectId || !diagnosis) return;
      const selectedProjectId = projectId;
      setLoading(true);
      setError("");
      try {
        await executeCommand(
          `generate-plan:${selectedProjectId}`,
          t.commands.generatePlan,
          "POST",
          `/projects/${selectedProjectId}/plan/generate`,
          {},
          async (bundle) => {
            setPlan(bundle);
            setPlanGenerated(true);
            setTaskStates({});
            setSubmissionsByTask({});
            setNotice(t.plan.generated);
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function transitionTask(task, nextStatus) {
      if (!projectId) return;
      const selectedProjectId = projectId;
      const expected = (taskStates[task.task_id] && taskStates[task.task_id].status) || task.status || "pending";
      setTaskBusyId(task.task_id);
      try {
        await executeCommand(
          `task-transition:${selectedProjectId}:${task.task_id}:${nextStatus}`,
          t.commands.transitionTask,
          "POST",
          `/projects/${selectedProjectId}/tasks/${task.task_id}/transition`,
          { expected_status: expected, next_status: nextStatus },
          async (updated) => {
            setTaskStates((previous) => ({ ...previous, [task.task_id]: { status: updated.status, verified: updated.verified } }));
            setNotice(nextStatus === "done" ? t.tasks.completed : t.tasks.started);
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setTaskBusyId("");
      }
    }

    async function submitTask(task, content, reset) {
      if (!projectId) return;
      const selectedProjectId = projectId;
      setTaskBusyId(task.task_id);
      try {
        await executeCommand(
          `task-submit:${selectedProjectId}:${task.task_id}`,
          t.commands.submitTask,
          "POST",
          `/projects/${selectedProjectId}/tasks/${task.task_id}/submissions`,
          { mode: "self_report", content },
          async (result) => {
            if (result && result.submission) {
              setSubmissionsByTask((previous) => ({
                ...previous,
                [task.task_id]: [...(previous[task.task_id] || []), { ...result.submission, task_id: task.task_id }],
              }));
            }
            if (reset) reset();
            setNotice(t.submission.recorded);
            // verified 只用服务端值：提交后重新读取任务详情，绝不由本地推断。
            const scope = { principalId, projectId: selectedProjectId, conversationId, epoch: scopeRef.current.epoch };
            const detail = await api("GET", `/projects/${selectedProjectId}/tasks/${task.task_id}`);
            if (isViewCurrent(scope)) {
              setTaskStates((previous) => ({ ...previous, [task.task_id]: { status: detail.status, verified: detail.verified } }));
            }
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setTaskBusyId("");
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
          t.commands.registerSource,
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
              t.commands.uploadSource,
              "POST",
              `/projects/${projectId}/sources/${source.source_id}/content`,
              contentBody,
              async (uploaded) => {
                setIngestionJobs((previous) => [uploaded.job, ...previous.filter((item) => item.job_id !== uploaded.job.job_id)]);
                setSourceForm({ displayName: "", title: "", content: "", mediaType: "text/markdown" });
                await refreshProjectData(projectId);
                setNotice(t.sources.registered);
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
          t.commands.registerCandidate,
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
            setNotice(t.sources.candidateAdded);
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
          t.commands.sourceSearch,
          "POST",
          `/projects/${projectId}/source-search`,
          { query, limit: 8 },
          async (result) => {
            const found = result.candidates || [];
            setCandidates((previous) => [
              ...found,
              ...previous.filter((item) => !found.some((candidate) => candidate.candidate_id === item.candidate_id)),
            ]);
            setNotice(found.length ? t.sources.found(found.length) : t.sources.noneFound);
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
          t.commands.selectCandidate,
          "POST",
          `/projects/${projectId}/source-candidates/${candidate.candidate_id}/select`,
          { display_name: candidate.title, media_type: "text/markdown", language: "zh" },
          async (result) => {
            setAcquisitionJobs((previous) => [result.acquisition, ...previous.filter((item) => item.acquisition_id !== result.acquisition.acquisition_id)]);
            await refreshProjectData(projectId);
            setNotice(t.sources.queuedNotice);
            pollAcquisitionJob(projectId, result.acquisition.acquisition_id).catch((caught) => setError(errorText(caught)));
          }
        );
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    async function addLibrarySource(event) {
      event.preventDefault();
      const name = libraryForm.name.trim();
      const url = libraryForm.url.trim();
      const content = libraryForm.content.trim();
      if (!name) {
        setLibraryError(t.library.invalidName);
        return;
      }
      if (!url && !content) {
        setLibraryError(t.library.invalidInput);
        return;
      }
      const body = {
        display_name: name,
        media_type: "text/markdown",
        acquisition: url ? { kind: "url", url, label: name } : { kind: "user_input", label: name },
      };
      if (content) body.content = content;
      setLibraryBusy(true);
      setLibraryError("");
      try {
        await executeCommand(
          `library-source:${name}`,
          t.commands.addLibrarySource,
          "POST",
          "/library/sources",
          body,
          async () => {
            setLibraryForm({ name: "", url: "", content: "" });
            setNotice(t.library.added);
            await refreshLibrary();
          }
        );
      } catch (caught) {
        setLibraryError(errorText(caught));
      } finally {
        setLibraryBusy(false);
      }
    }

    async function attachLibrarySource(source) {
      if (!projectId) return;
      const selectedProjectId = projectId;
      setLibraryBusyId(source.library_source_id);
      setLibraryError("");
      try {
        await executeCommand(
          `library-attach:${selectedProjectId}:${source.library_source_id}`,
          t.commands.attachLibrarySource,
          "POST",
          `/projects/${selectedProjectId}/library-sources/${source.library_source_id}/attach`,
          {},
          async (result) => {
            setNotice(result && result.created ? t.library.attached : t.library.alreadyAttached);
            await refreshProjectData(selectedProjectId);
            if (result && result.ingestion_job_id) {
              pollIngestionJob(selectedProjectId, result.ingestion_job_id).catch((caught) => setError(errorText(caught)));
            }
          }
        );
      } catch (caught) {
        setLibraryError(errorText(caught));
      } finally {
        setLibraryBusyId("");
      }
    }

    async function reloadLibrary() {
      setLibraryLoading(true);
      setLibraryError("");
      try {
        await refreshLibrary();
      } catch (caught) {
        setLibraryError(errorText(caught));
      } finally {
        setLibraryLoading(false);
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
        await refreshLibrary();
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
        setNotice(t.notices.refreshed);
      } catch (caught) {
        setError(errorText(caught));
      } finally {
        setLoading(false);
      }
    }

    if (authBusy && !user) return withTheme(h(LoadingScreen));
    if (!user) return withTheme(h(AuthScreen, {
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
    }));

    return withTheme(
      h("div", { className: "app-shell" },
        h("div", { className: "starfield", "aria-hidden": true }),
        h("div", { className: "starfield-glow", "aria-hidden": true }),
        h("header", { className: "topbar" },
          h("div", { className: "brand" },
            h("span", { className: "brand-mark", "aria-hidden": true }, t.app.brandMark),
            h("span", null, t.app.name)),
          h("div", { className: "topbar-actions" },
            h("span", { className: "identity" }, user.display_name || user.principal_id),
            h(antd.Button, {
              className: "icon-button",
              onClick: reloadWorkspace,
              disabled: loading,
              title: t.app.refreshing,
              "aria-label": t.app.refreshing,
            }, "↻"),
            h(antd.Button, { className: "quiet-button", onClick: () => setSettingsOpen(true) }, t.app.settings),
            h(antd.Button, { className: "quiet-button", onClick: logout }, t.app.logout))),
        h("div", { className: "workspace" },
          h(ProjectRail, {
            projects,
            projectId,
            setProjectId: (id) => { setConversationId(""); setProjectId(id); },
            newProject,
            setNewProject,
            createProject,
            loading,
            projectFormOpen,
            setProjectFormOpen,
          }, h(KnowledgeBasePanel, {
            sources: librarySources,
            loading: libraryLoading,
            error: libraryError,
            projectId,
            busy: libraryBusy,
            form: libraryForm,
            setForm: setLibraryForm,
            onAdd: addLibrarySource,
            onAttach: attachLibrarySource,
            onRefresh: reloadLibrary,
            attachedIds: { busyId: libraryBusyId },
          })),
          h("main", { className: "main-column" },
            error && h("div", { className: "alert alert-error", role: "alert" },
              h("span", null, error),
              h("button", { type: "button", onClick: () => setError(""), "aria-label": t.notices.closeError }, "×")),
            pendingCommand && h("div", { className: "alert alert-warning", role: "status" },
              h("span", null, t.notices.unknownResult(pendingCommand.label)),
              h(antd.Button, { className: "secondary-button compact", onClick: retryPendingCommand, disabled: loading },
                pendingCommand.retrying ? t.notices.retrying : t.notices.retrySame)),
            notice && h("div", { className: "alert alert-success", role: "status" }, notice),
            h("div", { className: "workspace-heading" },
              h("div", null,
                h("p", { className: "eyebrow" }, project ? t.workspace.eyebrowProject : t.workspace.eyebrowSpace),
                h("h1", null, project ? project.name : t.workspace.pickProject),
                project && h("p", { className: "subheading" }, project.goal || t.workspace.noGoal)),
              project && h("form", { className: "new-conversation", onSubmit: createConversation },
                h(antd.Input, {
                  value: newConversation,
                  onChange: (event) => setNewConversation(event.target.value),
                  placeholder: t.workspace.newConversationPlaceholder,
                  "aria-label": t.workspace.newConversationLabel,
                }),
                h(antd.Button, { type: "primary", htmlType: "submit", className: "compact", disabled: loading }, t.workspace.newConversation))),
            project && h(WorkspaceTabs, { view, setView }),
            project
              ? h(MainView, {
                view,
                projectId,
                conversations,
                conversationId,
                setConversationId,
                currentConversation,
                messages,
                question,
                setQuestion,
                askQuestion,
                loading,
                activeRun,
                plan,
                planForm,
                setPlanForm,
                savePlan,
                diagnosis,
                diagnosisForm,
                setDiagnosisForm,
                saveDiagnosis,
                generatePlan,
                planGenerated,
                taskStates,
                onTransitionTask: transitionTask,
                onSubmitTask: submitTask,
                submissionsByTask,
                taskBusyId,
                sources,
                ingestionJobs,
                candidates,
                acquisitionJobs,
                sourceForm,
                setSourceForm,
                saveSource,
                candidateForm,
                setCandidateForm,
                saveCandidate,
                sourceSearchQuery,
                setSourceSearchQuery,
                searchSourceCandidates,
                selectCandidate,
              })
              : h(EmptyProject, { onFocus: () => document.querySelector(".project-create summary")?.click() })),
          project && h(EvidenceRail, { activeRun, sources, projectId, plan, view, onReadCitation: readCitation, citationReading })),
        h(SettingsDrawer, {
          open: settingsOpen,
          onClose: () => setSettingsOpen(false),
          user,
          onLogout: logout,
          onReload: reloadWorkspace,
          loading,
        })));
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
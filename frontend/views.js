/* Presentational workbench views. 文案统一来自 window.StudyTerms。 */
/* global React, antd */
(function () {
  "use strict";
  const h = React.createElement;
  const { useState } = React;
  const { Button, Input, InputNumber, Select, Drawer, Tag, Tooltip, Divider } = antd;
  const t = window.StudyTerms;

  // ===== 基础构件 =====

  function StatusDot({ status }) {
    return h("span", { className: `status-dot ${status}`, "aria-hidden": true });
  }

  function LoadingScreen() {
    return h("div", { className: "center-screen" },
      h("div", { className: "starfield", "aria-hidden": true }),
      h("div", { className: "loading-mark", "aria-label": t.app.loading }, t.app.brandMark),
      h("p", null, t.app.loading));
  }

  function Brand({ large }) {
    return h("div", { className: `brand ${large ? "large" : ""}` },
      h("span", { className: "brand-mark", "aria-hidden": true }, t.app.brandMark),
      h("span", null, t.app.name));
  }

  // ===== 登录界面（独立构图）=====

  const AUTH_MODES = () => [["invite", t.auth.modes.invite], ["login", t.auth.modes.login], ["register", t.auth.modes.register]];

  function AuthScreen({ mode, setMode, token, setToken, credentials, setCredentials, onInvite, onPassword, busy, error }) {
    const passwordTooShort = mode === "register"
      && Boolean(credentials.password)
      && (credentials.password.length < 12 || credentials.password.length > 128);
    return h("main", { className: "auth-screen" },
      h("div", { className: "starfield", "aria-hidden": true }),
      h("section", { className: "auth-hero" },
        h(Brand, { large: true }),
        h("h1", null, t.auth.tagline),
        h("ul", { className: "auth-points" },
          h("li", null, t.diagnosis.title),
          h("li", null, t.plan.eyebrow),
          h("li", null, t.tasks.type),
          h("li", null, t.evidence.title))),
      h("section", { className: "auth-panel glass" },
        h("div", { className: "auth-switch", role: "tablist", "aria-label": t.auth.modeLabel },
          AUTH_MODES().map(([value, label]) => h("button", {
            key: value,
            type: "button",
            role: "tab",
            "aria-selected": mode === value,
            className: mode === value ? "active" : "",
            onClick: () => { setCredentials({ username: "", password: "" }); setToken(""); setMode(value); },
          }, label))),
        error && h("div", { className: "alert alert-error", role: "alert" }, error),
        mode === "invite"
          ? h("form", { className: "auth-form", onSubmit: onInvite },
            h("label", { htmlFor: "invite-token" }, t.auth.inviteLabel),
            h(Input, {
              id: "invite-token",
              value: token,
              size: "large",
              autoComplete: "one-time-code",
              placeholder: t.auth.invitePlaceholder,
              disabled: busy,
              onChange: (event) => setToken(event.target.value),
            }),
            h(Button, { type: "primary", htmlType: "submit", size: "large", block: true, className: "auth-submit", loading: busy, disabled: busy || !token.trim() },
              busy ? t.auth.entering : t.auth.enter))
          : h("form", { className: "auth-form", onSubmit: onPassword },
            h("label", { htmlFor: "auth-username" }, t.auth.usernameLabel),
            h(Input, {
              id: "auth-username",
              value: credentials.username,
              size: "large",
              autoComplete: "username",
              maxLength: 16,
              placeholder: t.auth.usernamePlaceholder,
              disabled: busy,
              onChange: (event) => setCredentials({ ...credentials, username: event.target.value }),
            }),
            h("label", { htmlFor: "auth-password" }, t.auth.passwordLabel),
            h(Input, {
              id: "auth-password",
              type: "password",
              value: credentials.password,
              size: "large",
              autoComplete: mode === "register" ? "new-password" : "current-password",
              minLength: mode === "register" ? 12 : 1,
              maxLength: 128,
              placeholder: mode === "register" ? t.auth.passwordPlaceholderRegister : t.auth.passwordPlaceholder,
              disabled: busy,
              "aria-describedby": mode === "register" ? "password-hint" : undefined,
              onChange: (event) => setCredentials({ ...credentials, password: event.target.value }),
            }),
            mode === "register" && h("p", { id: "password-hint", className: `field-hint ${passwordTooShort ? "field-error" : ""}` }, t.auth.passwordHint),
            h(Button, { type: "primary", htmlType: "submit", size: "large", block: true, className: "auth-submit", loading: busy, disabled: busy || !credentials.username.trim() || !credentials.password },
              busy ? t.auth.submitting : mode === "register" ? t.auth.createAccount : t.auth.login))));
  }

  // ===== 左栏：项目 =====

  function ProjectRail({ projects, projectId, setProjectId, newProject, setNewProject, createProject, loading, projectFormOpen, setProjectFormOpen, children }) {
    return h("aside", { className: "project-rail" },
      h("section", { className: "rail-section" },
        h("div", { className: "rail-heading" }, h("span", null, t.projects.heading), h("span", { className: "count" }, projects.length)),
        h("nav", { className: "project-list", "aria-label": t.projects.listLabel },
          projects.length
            ? projects.map((item) => h("button", {
              key: item.project_id,
              type: "button",
              className: `project-item ${item.project_id === projectId ? "selected" : ""}`,
              onClick: () => setProjectId(item.project_id),
            }, h("span", { className: "project-dot", "aria-hidden": true }, ""), h("span", null, item.name)))
            : h("p", { className: "rail-empty" }, t.projects.empty)),
        h("details", { className: "project-create", open: projectFormOpen, onToggle: (event) => setProjectFormOpen(event.currentTarget.open) },
          h("summary", null, t.projects.createSummary),
          h("form", { className: "project-form", onSubmit: createProject },
            h("label", { htmlFor: "project-name" }, t.projects.nameLabel),
            h(Input, { id: "project-name", value: newProject.name, placeholder: t.projects.namePlaceholder, onChange: (event) => setNewProject({ ...newProject, name: event.target.value }) }),
            h("label", { htmlFor: "project-goal" }, t.projects.goalLabel),
            h(Input.TextArea, { id: "project-goal", value: newProject.goal, rows: 3, placeholder: t.projects.goalPlaceholder, onChange: (event) => setNewProject({ ...newProject, goal: event.target.value }) }),
            h(Button, { type: "primary", htmlType: "submit", size: "small", className: "compact", disabled: loading || !newProject.name.trim() }, t.projects.create)))),
      children);
  }

  // ===== 左栏：知识库（用户级、跨项目共享）=====

  function KnowledgeBasePanel({ sources, loading, error, projectId, busy, form, setForm, onAdd, onAttach, onRefresh, attachedIds }) {
    const busyId = attachedIds && attachedIds.busyId;
    return h("section", { className: "rail-section library-section", "aria-label": t.library.title },
      h("div", { className: "rail-heading" }, h("span", null, t.library.title), h("span", { className: "count" }, sources.length)),
      h("p", { className: "rail-note" }, t.library.subtitle),
      error && h("div", { className: "alert alert-error", role: "alert" }, h("span", null, error)),
      h("form", { className: "library-form", onSubmit: onAdd },
        h("label", { htmlFor: "library-name" }, t.library.nameLabel),
        h(Input, { id: "library-name", size: "small", value: form.name, placeholder: t.library.namePlaceholder, onChange: (event) => setForm({ ...form, name: event.target.value }) }),
        h("label", { htmlFor: "library-url" }, t.library.urlLabel),
        h(Input, { id: "library-url", size: "small", value: form.url, placeholder: t.library.urlPlaceholder, onChange: (event) => setForm({ ...form, url: event.target.value }) }),
        h("label", { htmlFor: "library-content" }, t.library.contentLabel),
        h(Input.TextArea, { id: "library-content", rows: 3, value: form.content, placeholder: t.library.contentPlaceholder, onChange: (event) => setForm({ ...form, content: event.target.value }) }),
        h(Button, { type: "primary", htmlType: "submit", size: "small", className: "compact", loading: busy, disabled: busy || !form.name.trim() }, busy ? t.library.adding : t.library.add)),
      !projectId && h("p", { className: "field-hint" }, t.library.noProject),
      loading && !sources.length
        ? h("p", { className: "rail-empty" }, t.library.loading)
        : h("div", { className: "library-list" },
          sources.length
            ? sources.map((source) => h("div", { className: "library-item", key: source.library_source_id },
              h("div", { className: "library-item-head" },
                h("strong", null, source.display_name),
                h(Tag, { color: source.has_content ? "cyan" : "default", className: "library-tag" },
                  source.has_content ? t.library.hasContent : t.library.noContent)),
              h("span", { className: "library-meta" }, source.media_type || t.sources.untyped),
              h(Tooltip, { title: projectId ? "" : t.library.noProject },
                h("span", { className: "button-wrap" },
                  h(Button, {
                    size: "small",
                    className: "compact library-attach",
                    type: "default",
                    disabled: !projectId || busyId === source.library_source_id,
                    loading: busyId === source.library_source_id,
                    onClick: () => onAttach(source),
                  }, busyId === source.library_source_id ? t.library.attaching : t.library.attach)))))
            : h("div", { className: "library-empty" }, h("strong", null, t.library.emptyTitle), h("p", null, t.library.emptyCopy))),
      h("div", { className: "library-actions" },
        h(Button, { size: "small", className: "compact", onClick: onRefresh, loading: loading }, t.library.refresh)));
  }

  // ===== 主区骨架 =====

  function WorkspaceTabs({ view, setView }) {
    return h("nav", { className: "tabs", "aria-label": t.tabs.label }, [
      ["conversation", t.tabs.conversation], ["plan", t.tabs.plan], ["sources", t.tabs.sources],
    ].map(([value, label]) => h("button", {
      key: value,
      type: "button",
      className: view === value ? "active" : "",
      onClick: () => setView(value),
    }, label)));
  }

  function EmptyProject({ onFocus }) {
    return h("section", { className: "empty-project" },
      h("div", { className: "empty-glyph", "aria-hidden": true }, "＋"),
      h("h2", null, t.projects.emptyTitle),
      h("p", null, t.projects.emptyCopy),
      h(Button, { type: "primary", onClick: onFocus }, t.projects.emptyAction));
  }

  function MainView(props) {
    if (props.view === "plan") return h(PlanView, props);
    if (props.view === "sources") return h(SourcesView, props);
    return h(ConversationView, props);
  }

  // ===== 会话 =====

  function ConversationView({ conversations, conversationId, setConversationId, currentConversation, messages, question, setQuestion, askQuestion, loading, activeRun }) {
    return h("section", { className: "content-section conversation-view" },
      conversations.length > 0 && h("div", { className: "conversation-picker" },
        h("label", { htmlFor: "conversation-select" }, t.conversation.pickerLabel),
        h("select", { id: "conversation-select", className: "conversation-select", value: conversationId, onChange: (event) => setConversationId(event.target.value) },
          conversations.map((item) => h("option", { key: item.conversation_id, value: item.conversation_id }, item.title || t.conversation.unnamed)))),
      !currentConversation
        ? h("div", { className: "empty-state" }, h("h2", null, t.conversation.noneTitle), h("p", null, t.conversation.noneCopy))
        : h(React.Fragment, null,
          h("div", { className: "message-stream" },
            messages.length
              ? messages.map((message) => h(MessageBubble, { key: message.message_id, message }))
              : h("div", { className: "empty-state compact-empty" }, h("h2", null, t.conversation.startTitle), h("p", null, t.conversation.startCopy))),
          activeRun && h(RunStrip, { run: activeRun }),
          h("form", { className: "question-form", onSubmit: askQuestion },
            h(Input.TextArea, {
              value: question,
              rows: 3,
              "aria-label": t.conversation.inputLabel,
              placeholder: t.conversation.inputPlaceholder,
              disabled: loading,
              onChange: (event) => setQuestion(event.target.value),
            }),
            h("div", { className: "question-actions" },
              h("span", { className: "field-hint" }, t.conversation.inputHint),
              h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading || !question.trim() },
                loading ? t.conversation.sending : t.conversation.send)))));
  }

  function MessageBubble({ message }) {
    const who = message.role === "user" ? t.messages.you : message.role === "assistant" ? t.messages.assistant : t.messages.system;
    return h("article", { className: `message ${message.role}` },
      h("div", { className: "message-meta" }, who),
      h("div", { className: "message-content" }, message.content));
  }

  function RunStrip({ run }) {
    return h("div", { className: `run-strip ${run.status}` },
      h(StatusDot, { status: run.status }),
      h("span", null, t.run[run.status] || run.status),
      run.error_detail && h("span", { className: "run-detail" }, run.error_detail));
  }

  // ===== 计划：诊断 → 生成 → 任务流转 → 反馈 =====

  function DiagnosisPanel({ diagnosis, diagnosisForm, setDiagnosisForm, saveDiagnosis, loading }) {
    if (diagnosis) {
      const answers = diagnosis.answers || {};
      return h("section", { className: "panel glass" },
        h("div", { className: "panel-heading" }, h("h3", null, t.diagnosis.title), h(Tag, { color: "cyan" }, t.diagnosis.done)),
        h("dl", { className: "diagnosis-summary" },
          h("dt", null, t.diagnosis.experienceLabel), h("dd", null, t.diagnosis.levels[answers.experience_level] || "—"),
          h("dt", null, t.diagnosis.hoursLabel), h("dd", null, `${answers.weekly_hours} ${t.diagnosis.hoursSuffix}`),
          h("dt", null, t.diagnosis.styleLabel), h("dd", null, t.diagnosis.styles[answers.preferred_style] || "—")),
        diagnosis.summary && h("p", { className: "field-hint" }, diagnosis.summary));
    }
    return h("section", { className: "panel glass" },
      h("div", { className: "panel-heading" }, h("h3", null, t.diagnosis.title), h("span", { className: "field-hint" }, t.diagnosis.required)),
      h("p", { className: "field-hint" }, t.diagnosis.copy),
      h("form", { className: "diagnosis-form", onSubmit: saveDiagnosis },
        h("label", { htmlFor: "diagnosis-level" }, t.diagnosis.experienceLabel),
        h(Select, {
          id: "diagnosis-level",
          value: diagnosisForm.experience_level,
          className: "diagnosis-select",
          onChange: (value) => setDiagnosisForm({ ...diagnosisForm, experience_level: value }),
          options: Object.keys(t.diagnosis.levels).map((value) => ({ value, label: t.diagnosis.levels[value] })),
        }),
        h("label", { htmlFor: "diagnosis-hours" }, t.diagnosis.hoursLabel),
        h(InputNumber, {
          id: "diagnosis-hours",
          min: 1,
          max: 40,
          value: diagnosisForm.weekly_hours,
          onChange: (value) => setDiagnosisForm({ ...diagnosisForm, weekly_hours: value }),
        }),
        h("label", { htmlFor: "diagnosis-style" }, t.diagnosis.styleLabel),
        h(Select, {
          id: "diagnosis-style",
          value: diagnosisForm.preferred_style,
          className: "diagnosis-select",
          onChange: (value) => setDiagnosisForm({ ...diagnosisForm, preferred_style: value }),
          options: Object.keys(t.diagnosis.styles).map((value) => ({ value, label: t.diagnosis.styles[value] })),
        }),
        h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading }, loading ? t.diagnosis.submitting : t.diagnosis.submit)));
  }

  function GeneratePlanPanel({ diagnosis, loading, hasPlan, onGenerate }) {
    const disabled = loading || !diagnosis;
    return h("section", { className: "panel glass" },
      h("div", { className: "panel-heading" }, h("h3", null, t.plan.eyebrow), h("span", { className: "field-hint" }, diagnosis ? t.plan.generateReady : t.diagnosis.required)),
      h("p", { className: "field-hint" }, t.plan.generateRequired),
      h(Tooltip, { title: diagnosis ? "" : t.plan.generateNeedsDiagnosis },
        h("span", { className: "button-wrap" },
          h(Button, { type: hasPlan ? "default" : "primary", onClick: onGenerate, loading, disabled }, loading ? t.plan.generating : hasPlan ? t.plan.regenerate : t.plan.generate))));
  }

  function hasGeneratedFields(task) {
    return Boolean(task && (task.task_type || task.objective || task.instruction || task.deliverable || task.acceptance_criteria || task.estimated_minutes));
  }

  function isInteractiveBundle(bundle, generatedFlag) {
    if (generatedFlag) return true;
    if (!bundle) return false;
    if (bundle.generator) return true;
    return (bundle.tasks || []).some(hasGeneratedFields);
  }

  function TaskCard({ task, state, interactive, busy, onTransition, onSubmit, submissions }) {
    const [draft, setDraft] = useState("");
    const [open, setOpen] = useState(false);
    const status = (state && state.status) || task.status || "pending";
    const verified = Boolean(state && state.verified);
    const canStart = interactive && status === "pending";
    const canComplete = interactive && status === "in_progress";
    const canSubmit = interactive && status !== "pending";
    const mine = (submissions || []).filter((item) => item.task_id === task.task_id);
    return h("article", { className: `task-card glass ${status}` },
      h("div", { className: "task-card-head" },
        h(StatusDot, { status }),
        h("strong", { className: "task-title" }, task.title),
        h(Tag, { className: `task-status-tag ${status}` }, t.tasks.status[status] || status),
        interactive && h(Tag, { color: verified ? "green" : "default" }, verified ? t.tasks.verified : t.tasks.unverified)),
      task.objective && h("p", { className: "task-field" }, h("span", null, t.tasks.objective), task.objective),
      task.instruction && h("p", { className: "task-field" }, h("span", null, t.tasks.instruction), task.instruction),
      task.deliverable && h("p", { className: "task-field" }, h("span", null, t.tasks.deliverable), task.deliverable),
      task.acceptance_criteria && h("p", { className: "task-field" }, h("span", null, t.tasks.acceptance), task.acceptance_criteria),
      h("div", { className: "task-meta" },
        task.task_type && h(Tag, { className: "task-type-tag" }, task.task_type),
        task.estimated_minutes && h("span", { className: "field-hint" }, `${t.tasks.estimated} ${t.tasks.minutes(task.estimated_minutes)}`)),
      h("div", { className: "task-actions" },
        canStart && h(Button, { size: "small", className: "compact", loading: busy, disabled: busy, onClick: () => onTransition(task, "in_progress") }, t.tasks.start),
        canComplete && h(Button, { size: "small", className: "compact", type: "primary", loading: busy, disabled: busy, onClick: () => onTransition(task, "done") }, t.tasks.complete),
        canSubmit && h(Button, { size: "small", className: "compact", onClick: () => setOpen(!open) }, open ? t.submission.cancel : t.submission.title),
        !interactive && h("span", { className: "field-hint" }, t.tasks.readOnly)),
      open && canSubmit && h("form", {
        className: "submission-form",
        onSubmit: (event) => {
          event.preventDefault();
          if (!draft.trim()) return;
          onSubmit(task, draft.trim(), () => setDraft(""));
        },
      },
        h("p", { className: "self-report-note" }, h("strong", null, t.submission.selfReport), `：${t.submission.selfReportNotice}`),
        h(Input.TextArea, {
          rows: 3,
          value: draft,
          "aria-label": t.submission.label,
          placeholder: t.submission.placeholder,
          onChange: (event) => setDraft(event.target.value),
        }),
        h(Button, { type: "primary", htmlType: "submit", size: "small", className: "compact", loading: busy, disabled: busy || !draft.trim() }, busy ? t.submission.submitting : t.submission.submit)),
      mine.length > 0 && h("div", { className: "submission-list" },
        h("strong", null, t.submission.listTitle),
        mine.map((item) => h("p", { key: item.submission_id || item.content },
          h("span", { className: "submission-badge" }, t.submission.selfReport), item.content)),
        h("p", { className: "field-hint" }, t.submission.selfReportNotice)));
  }

  function PlanView(props) {
    const { plan, planForm, setPlanForm, savePlan, loading, diagnosis, diagnosisForm, setDiagnosisForm, saveDiagnosis, generatePlan, planGenerated, taskStates, onTransitionTask, onSubmitTask, submissionsByTask, taskBusyId } = props;
    const milestones = (plan && plan.milestones) || [];
    const tasks = (plan && plan.tasks) || [];
    const byMilestone = new Map();
    tasks.forEach((task) => {
      if (!byMilestone.has(task.milestone_id)) byMilestone.set(task.milestone_id, []);
      byMilestone.get(task.milestone_id).push(task);
    });
    // 任务与里程碑的分组依赖服务端在任务里带 milestone_id。当前响应可能没有
    // 这个字段（并行轨道仍在补），所以未匹配到任何里程碑的任务必须仍然可见，
    // 否则计划看起来"有里程碑没任务"。
    const grouped = new Set();
    milestones.forEach((milestone) => {
      (byMilestone.get(milestone.milestone_id) || []).forEach((task) => grouped.add(task.task_id));
    });
    const ungroupedTasks = tasks.filter((task) => !grouped.has(task.task_id));
    const interactive = isInteractiveBundle(plan, planGenerated);
    const renderTask = (task) => h(TaskCard, {
      key: task.task_id,
      task,
      state: taskStates[task.task_id],
      interactive,
      busy: taskBusyId === task.task_id,
      onTransition: onTransitionTask,
      onSubmit: onSubmitTask,
      submissions: submissionsByTask[task.task_id],
    });
    return h("section", { className: "content-section form-section" },
      h("div", { className: "section-title" },
        h("div", null,
          h("p", { className: "eyebrow" }, t.plan.eyebrow),
          h("h2", null, plan ? t.plan.version(plan.plan && plan.plan.version) : t.plan.none))),
      h(DiagnosisPanel, { diagnosis, diagnosisForm, setDiagnosisForm, saveDiagnosis, loading }),
      h(GeneratePlanPanel, { diagnosis, loading, hasPlan: Boolean(plan), onGenerate: generatePlan }),
      plan
        ? h(React.Fragment, null,
          h("p", { className: "read-only-note" }, interactive ? t.plan.generatedNote : t.plan.readOnlyNote),
          h("div", { className: "plan-summary" },
            milestones.map((milestone) => h("div", { className: "milestone-block", key: milestone.milestone_id },
              h("div", { className: "milestone-row" }, h(StatusDot, { status: "queued" }), h("strong", null, milestone.title)),
              (byMilestone.get(milestone.milestone_id) || []).map(renderTask))),
            ungroupedTasks.map(renderTask)))
        : h("div", { className: "manual-plan" },
          h("h3", { className: "subsection-title" }, t.plan.manualTitle),
          h("form", { className: "plan-form", onSubmit: savePlan },
            h("label", { htmlFor: "plan-goal" }, t.plan.goalLabel),
            h(Input.TextArea, { id: "plan-goal", rows: 4, value: planForm.goal, placeholder: t.plan.goalPlaceholder, onChange: (event) => setPlanForm({ ...planForm, goal: event.target.value }) }),
            h("label", { htmlFor: "plan-milestone" }, t.plan.milestoneLabel),
            h(Input, { id: "plan-milestone", value: planForm.milestone, placeholder: t.plan.milestonePlaceholder, onChange: (event) => setPlanForm({ ...planForm, milestone: event.target.value }) }),
            h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading || !planForm.goal.trim() || !planForm.milestone.trim() }, loading ? t.plan.creating : t.plan.create))));
  }

  // ===== 资料 =====

  function SourcesView({ sources, ingestionJobs, candidates, acquisitionJobs, sourceForm, setSourceForm, saveSource, candidateForm, setCandidateForm, saveCandidate, sourceSearchQuery, setSourceSearchQuery, searchSourceCandidates, selectCandidate, loading }) {
    const latestJobs = new Map();
    (ingestionJobs || []).forEach((job) => {
      if (!latestJobs.has(job.source_id)) latestJobs.set(job.source_id, job);
    });
    return h("section", { className: "content-section form-section" },
      h("div", { className: "section-title" },
        h("div", null, h("p", { className: "eyebrow" }, t.sources.eyebrow), h("h2", null, t.sources.count(sources.length)))),
      h("form", { className: "source-search-form", onSubmit: searchSourceCandidates },
        h("label", { htmlFor: "source-search-query" }, t.sources.searchLabel),
        h("div", { className: "source-search-controls" },
          h(Input, { id: "source-search-query", value: sourceSearchQuery, maxLength: 2000, placeholder: t.sources.searchPlaceholder, required: true, onChange: (event) => setSourceSearchQuery(event.target.value) }),
          h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading || !sourceSearchQuery.trim() }, loading ? t.sources.searching : t.sources.search))),
      h("form", { className: "source-form candidate-form", onSubmit: saveCandidate },
        h("label", { htmlFor: "candidate-url" }, t.sources.candidateUrlLabel),
        h(Input, { id: "candidate-url", type: "url", value: candidateForm.url, placeholder: t.sources.candidateUrlPlaceholder, required: true, onChange: (event) => setCandidateForm({ ...candidateForm, url: event.target.value }) }),
        h("label", { htmlFor: "candidate-title" }, t.sources.candidateTitleLabel),
        h(Input, { id: "candidate-title", value: candidateForm.title, placeholder: t.sources.candidateTitlePlaceholder, required: true, onChange: (event) => setCandidateForm({ ...candidateForm, title: event.target.value }) }),
        h("label", { htmlFor: "candidate-snippet" }, t.sources.candidateSnippetLabel),
        h(Input.TextArea, { id: "candidate-snippet", rows: 3, value: candidateForm.snippet, placeholder: t.sources.candidateSnippetPlaceholder, onChange: (event) => setCandidateForm({ ...candidateForm, snippet: event.target.value }) }),
        h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading || !candidateForm.url.trim() || !candidateForm.title.trim() }, loading ? t.sources.registering : t.sources.registerCandidate)),
      h("div", { className: "source-list candidate-list" },
        candidates && candidates.length
          ? candidates.map((candidate) => {
            const acquisition = (acquisitionJobs || []).find((job) => job.candidate_id === candidate.candidate_id);
            const status = acquisition
              ? (t.sources.acquisition[acquisition.status] || acquisition.status)
              : candidate.status === "selected" ? t.sources.acquisition.selected : t.sources.acquisition.pending;
            return h("div", { className: "source-row candidate-row", key: candidate.candidate_id },
              h("div", null,
                h("strong", null, candidate.title),
                h("a", { href: candidate.url, target: "_blank", rel: "noreferrer" }, candidate.source_domain),
                candidate.snippet && h("span", null, candidate.snippet)),
              h("div", { className: "candidate-actions" },
                h("span", { className: `source-status ${acquisition ? acquisition.status : candidate.status}` }, status),
                candidate.status === "discovered" && h(Button, { size: "small", className: "compact", loading: loading, disabled: loading, onClick: () => selectCandidate(candidate) }, t.sources.confirmDownload)));
          })
          : h("div", { className: "empty-state compact-empty" }, h("h2", null, t.sources.noCandidatesTitle), h("p", null, t.sources.noCandidatesCopy))),
      h("div", { className: "source-divider", "aria-hidden": true }),
      h("h2", { className: "subsection-title" }, t.sources.uploadedTitle),
      h("form", { className: "source-form", onSubmit: saveSource },
        h("label", { htmlFor: "source-display" }, t.sources.displayLabel),
        h(Input, { id: "source-display", value: sourceForm.displayName, placeholder: t.sources.displayPlaceholder, onChange: (event) => setSourceForm({ ...sourceForm, displayName: event.target.value }) }),
        h("label", { htmlFor: "source-title" }, t.sources.docTitleLabel),
        h(Input, { id: "source-title", value: sourceForm.title, placeholder: t.sources.docTitlePlaceholder, onChange: (event) => setSourceForm({ ...sourceForm, title: event.target.value }) }),
        h("label", { htmlFor: "source-content" }, t.sources.contentLabel),
        h(Input.TextArea, { id: "source-content", rows: 8, value: sourceForm.content, placeholder: t.sources.contentPlaceholder, onChange: (event) => setSourceForm({ ...sourceForm, content: event.target.value }) }),
        h(Button, { type: "primary", htmlType: "submit", loading: loading, disabled: loading || !sourceForm.content.trim() }, loading ? t.sources.registering : t.sources.register)),
      h("div", { className: "source-list" },
        sources.length
          ? sources.map((source) => {
            const job = latestJobs.get(source.source_id);
            const status = job ? (t.sources.ingestion[job.status] || job.status) : t.sources.ingestion.registered;
            return h("div", { className: "source-row", key: source.source_id },
              h("div", null,
                h("strong", null, source.display_name),
                h("span", null, source.media_type || t.sources.untyped),
                job && h("span", null, t.sources.version(job.document_id))),
              h("span", { className: `source-status ${job ? job.status : "registered"}` }, status));
          })
          : h("div", { className: "empty-state compact-empty" }, h("h2", null, t.sources.emptyTitle), h("p", null, t.sources.emptyCopy))));
  }

  // ===== 右栏：证据（window.StudyViews.EvidenceRail —— P9 浏览器门禁契约）=====

  function EvidenceRail({ activeRun, sources, plan, view, onReadCitation, citationReading }) {
    const statusLabel = activeRun && activeRun.status === "succeeded"
      ? activeRun.grounding === "sourced" ? t.evidence.verified : t.evidence.general
      : activeRun && activeRun.status === "reconciliation_required"
        ? t.evidence.reconciliation
        : t.evidence.runStatus;
    const rewriteStatus = activeRun && activeRun.routing_decision && activeRun.routing_decision.query_rewrite_status;
    // 注意：这是**查询改写**的路由标签，不是检索模式 —— 检索模式由
    // 下方的 retrievalLabel 单独展示，两者是不同的降级概念。
    const routeLabel = rewriteStatus === "applied"
      ? t.routing.applied
      : rewriteStatus === "fallback"
        ? t.routing.fallback
        : rewriteStatus === "disabled"
          ? t.routing.disabled
          : "";
    const retrieval = activeRun && activeRun.retrieval_decision;
    const retrievalLabel = retrieval
      ? retrieval.mode === "hybrid"
        ? t.retrieval.hybrid
        : retrieval.mode === "degraded"
          ? t.retrieval.degraded(t.retrieval.reasons[retrieval.reason_code] || t.retrieval.unknownReason)
          : t.retrieval.keyword
      : "";
    const evidence = activeRun
      ? h(React.Fragment, null,
        h("div", { className: "evidence-status" }, h(StatusDot, { status: activeRun.status }), h("strong", null, statusLabel)),
        routeLabel && h("p", { className: "routing-mode" }, routeLabel),
        retrievalLabel && h("p", { className: "routing-mode" }, retrievalLabel),
        activeRun.grounding && h("p", { className: "grounding" }, activeRun.grounding === "sourced" ? t.evidence.groundingSourced : t.evidence.groundingUnsourced),
        activeRun.citations && activeRun.citations.length
          ? h("div", { className: "citation-list" }, activeRun.citations.map((citation, index) => {
            const key = `${citation.document_id}:${citation.span_start}:${citation.span_end}`;
            const reading = citationReading && citationReading.key === key ? citationReading : null;
            const source = sources.find((item) => item.source_id === citation.source_id);
            return h("div", { className: "citation", key: `${citation.source_id}-${index}` },
              h("span", { className: "citation-index" }, String(index + 1).padStart(2, "0")),
              h("div", null,
                h("strong", null, (source && source.display_name) || citation.source_id),
                h("span", null, t.evidence.span(citation.span_start, citation.span_end)),
                h("code", null, citation.content_hash),
                h(Button, { size: "small", className: "compact citation-button", loading: Boolean(reading && reading.loading), disabled: Boolean(reading && reading.loading), onClick: () => onReadCitation(citation) },
                  reading && reading.loading ? t.evidence.reading : t.evidence.read),
                reading && reading.error && h("span", { className: "citation-error" }, reading.error),
                reading && reading.result && h("blockquote", { className: "citation-content" }, reading.result.content)));
          }))
          : h("div", { className: "empty-rail" }, t.evidence.emptyCitations))
      : h("div", { className: "empty-rail" }, sources.length
        ? t.evidence.emptyRailRegistered(sources.length)
        : plan ? t.evidence.emptyRailPlan : t.evidence.emptyRailSelect);
    return h("aside", { className: "evidence-rail" },
      h("div", { className: "rail-heading" }, h("span", null, view === "sources" ? t.evidence.sourcesTitle : t.evidence.title)),
      evidence);
  }

  // ===== 设置抽屉 =====

  function SettingsDrawer({ open, onClose, user, onLogout, onReload, loading }) {
    return h(Drawer, {
      open,
      onClose,
      placement: "right",
      width: 360,
      className: "settings-drawer",
      title: t.app.settings,
    },
      h("section", { className: "settings-section" },
        h("h3", null, t.app.accountTitle),
        h("dl", { className: "settings-list" },
          h("dt", null, t.auth.usernameLabel), h("dd", null, (user && user.display_name) || "—"),
          h("dt", null, "账号标识"), h("dd", null, (user && user.principal_id) || "—"),
          h("dt", null, "租户"), h("dd", null, (user && user.tenant_id) || "—"))),
      h(Divider),
      h("section", { className: "settings-section" },
        h("h3", null, t.app.themeTitle),
        h("p", { className: "field-hint" }, t.app.themeNote)),
      h(Divider),
      h("div", { className: "settings-actions" },
        h(Button, { block: true, onClick: onReload, loading, disabled: loading }, t.app.reload),
        h(Button, { block: true, danger: true, className: "settings-logout", onClick: onLogout }, t.app.logoutFull)));
  }

  window.StudyViews = {
    LoadingScreen,
    AuthScreen,
    ProjectRail,
    KnowledgeBasePanel,
    WorkspaceTabs,
    EmptyProject,
    MainView,
    ConversationView,
    PlanView,
    SourcesView,
    EvidenceRail,
    SettingsDrawer,
    StatusDot,
  };
})();
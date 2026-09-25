/* Project-scoped data loading: conversations, plan, diagnosis, sources and job lists. */
/* global React */
(function () {
  "use strict";

  const { useCallback, useMemo, useRef, useState } = React;
  const { api, savedSelection } = window.StudyApi;

  // useProjectState owns the project data lists and their read logic. Every
  // refresh guards against scope changes so a late response from an old
  // project/conversation can never overwrite the current view.
  function useProjectState({
    principalId,
    conversationId,
    setProjectId,
    setConversationId,
    isProjectCurrent,
    isViewCurrent,
    scopeRef,
    onProjectDataLoaded,
  }) {
    const [projects, setProjects] = useState([]);
    const [project, setProject] = useState(null);
    const [conversations, setConversations] = useState([]);
    const [messages, setMessages] = useState([]);
    const [plan, setPlan] = useState(null);
    const [diagnosis, setDiagnosis] = useState(null);
    const [sources, setSources] = useState([]);
    const [ingestionJobs, setIngestionJobs] = useState([]);
    const [candidates, setCandidates] = useState([]);
    const [acquisitionJobs, setAcquisitionJobs] = useState([]);
    // 知识库是**用户级**列表（跨项目共享），与 projectId 无关，
    // 只受账号 epoch 约束：切账号时旧响应不得覆盖新账号的库。
    const [librarySources, setLibrarySources] = useState([]);

    const loadedRef = useRef(onProjectDataLoaded);
    loadedRef.current = onProjectDataLoaded;

    const currentConversation = useMemo(
      () => conversations.find((item) => item.conversation_id === conversationId) || null,
      [conversations, conversationId]
    );

    const refreshProjects = useCallback(async (owner = scopeRef.current.principalId) => {
      const epoch = scopeRef.current.epoch;
      const result = await api("GET", "/projects");
      if (scopeRef.current.principalId !== owner || scopeRef.current.epoch !== epoch) return [];
      const next = result.projects || [];
      setProjects(next);
      setProjectId((previous) => {
        const candidate = previous || savedSelection(owner).projectId;
        return next.some((item) => item.project_id === candidate) ? candidate : next[0]?.project_id || "";
      });
      return next;
    }, [setProjectId]);

    const clearProjectData = useCallback(() => {
      setProject(null);
      setConversations([]);
      setMessages([]);
      setPlan(null);
      setDiagnosis(null);
      setSources([]);
      setIngestionJobs([]);
      setCandidates([]);
      setAcquisitionJobs([]);
    }, []);

    const refreshLibrary = useCallback(async () => {
      const epoch = scopeRef.current.epoch;
      const owner = scopeRef.current.principalId;
      const result = await api("GET", "/library/sources");
      if (scopeRef.current.principalId !== owner || scopeRef.current.epoch !== epoch) return [];
      const next = (result && result.sources) || [];
      setLibrarySources(next);
      return next;
    }, [scopeRef]);

    const refreshProjectData = useCallback(async (selectedId, signal) => {
      if (!selectedId) {
        clearProjectData();
        setConversationId("");
        return;
      }
      const scope = { principalId, projectId: selectedId, epoch: scopeRef.current.epoch };
      const [projectData, conversationData, planData, diagnosisData, sourceData, jobData, candidateData] = await Promise.all([
        api("GET", `/projects/${selectedId}`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/conversations`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/plan`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/diagnosis`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/sources`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/ingestion-jobs`, undefined, { signal }),
        api("GET", `/projects/${selectedId}/source-candidates`, undefined, { signal }),
      ]);
      if (!isProjectCurrent(scope)) return;
      const nextConversations = conversationData.conversations || [];
      setProject(projectData);
      setConversations(nextConversations);
      setPlan(planData);
      setDiagnosis(diagnosisData);
      setSources(sourceData.sources || []);
      setIngestionJobs(jobData.jobs || []);
      setCandidates(candidateData.candidates || []);
      setConversationId((previous) =>
        nextConversations.some((item) => item.conversation_id === previous)
          ? previous
          : nextConversations.find((item) => item.conversation_id === savedSelection(principalId).conversationId)?.conversation_id || nextConversations[0]?.conversation_id || ""
      );
      loadedRef.current?.(projectData, planData);
    }, [principalId, clearProjectData, setConversationId, isProjectCurrent]);

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
      if (!isViewCurrent(scope)) return;
      setMessages(result.messages || []);
    }, [principalId, isViewCurrent]);

    return {
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
      setLibrarySources,
      refreshLibrary,
      currentConversation,
      refreshProjects,
      refreshProjectData,
      refreshMessages,
      clearProjectData,
    };
  }

  window.StudyProjectState = { useProjectState };
})();

/* Scoped command execution: epoch guards, command registry and unknown-result retry. */
/* global React */
(function () {
  "use strict";

  const { useCallback, useRef, useState } = React;
  const { ApiError, requestKey, api } = window.StudyApi;

  // useScopedCommands owns the scope epoch, the in-flight command registry and
  // the pending (unknown-result) command state. Retrying an unknown command
  // reuses the original Idempotency-Key so the backend can deduplicate.
  function useScopedCommands({ principalId, projectId, conversationId, onNotice, onError, onLoading, errorText }) {
    const [pendingCommand, setPendingCommand] = useState(null);
    const scopeRef = useRef({ key: "", principalId: "", projectId: "", conversationId: "", epoch: 0 });
    const commandRef = useRef(new Map());

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
    const setNotice = (value) => { if (scopeRef.current.epoch === renderEpoch) onNotice(value); };
    const setError = (value) => { if (scopeRef.current.epoch === renderEpoch) onError(value); };
    const setLoading = (value) => { if (scopeRef.current.epoch === renderEpoch) onLoading(value); };

    const isProjectCurrent = useCallback((scope) => scopeRef.current.principalId === scope.principalId
      && scopeRef.current.projectId === scope.projectId
      && (scope.epoch === undefined || scopeRef.current.epoch === scope.epoch), []);

    const isViewCurrent = useCallback((scope) => isProjectCurrent(scope)
      && scopeRef.current.conversationId === scope.conversationId, [isProjectCurrent]);

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
        if (!(caught instanceof ApiError) || caught.status >= 500) {
          command.state = "unknown";
          if (isViewCurrent(scope)) setPendingCommand({ ...command, retrying: false });
        } else {
          commandRef.current.delete(name);
        }
        throw caught;
      }
      commandRef.current.delete(name);
      if (!isViewCurrent(scope)) return;
      setPendingCommand(null);
      return await onSuccess(result);
    }

    async function retryPendingCommand() {
      if (!pendingCommand || pendingCommand.retrying) return;
      if (!isViewCurrent(pendingCommand.scope)) return;
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

    const dropCommand = useCallback((name) => {
      commandRef.current.delete(name);
      setPendingCommand(null);
    }, []);

    const clearPendingCommand = useCallback(() => {
      setPendingCommand(null);
    }, []);

    const clearCommandRegistry = useCallback(() => {
      commandRef.current.clear();
    }, []);

    const resetCommands = useCallback(() => {
      commandRef.current.clear();
      setPendingCommand(null);
    }, []);

    return {
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
    };
  }

  window.StudyCommands = { useScopedCommands };
})();

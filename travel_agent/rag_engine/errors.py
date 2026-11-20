"""用明确的异常区分未完成练习、参数错误和预算不足。"""


class ExercisePending(NotImplementedError):
    def __init__(self, exercise_id: str, hint: str):
        self.exercise_id = exercise_id
        self.hint = hint
        super().__init__(f"[{exercise_id}] 尚未实现：{hint}")


class ContextBudgetError(ValueError):
    """必要的系统指令、任务与输出约束已经超过可用预算。"""


class WorkspaceViolation(ValueError):
    """请求的文件路径越过了允许读取的工作区。"""


class ModelNotConfigured(RuntimeError):
    """没有可用的真实模型，或测试替身的回复已用完。"""

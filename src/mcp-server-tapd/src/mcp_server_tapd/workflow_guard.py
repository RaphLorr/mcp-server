"""Validate state changes against the target project's actual workflow."""

import json


class WorkflowValidationError(ValueError):
    def __init__(self, message, **details):
        super().__init__(
            json.dumps(
                {
                    "error": "workflow_validation_failed",
                    "message": message,
                    "updated": False,
                    **details,
                },
                ensure_ascii=False,
            )
        )


def _read(client, method, params):
    try:
        response = getattr(client, method)(dict(params))
    except Exception:
        # Provider exceptions may contain credential-bearing URLs. Never echo them.
        raise WorkflowValidationError(
            "无法读取项目工作流或票据，未执行写入", query=method
        ) from None
    if (
        not isinstance(response, dict)
        or response.get("status") != 1
        or "data" not in response
    ):
        raise WorkflowValidationError(
            "项目工作流或票据查询失败，未执行写入", query=method
        )
    return response["data"]


def _state_map(value, query):
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in value.items()
        )
    ):
        raise WorkflowValidationError(
            "状态配置为空或格式不支持，未执行写入", query=query
        )
    return value


def validate_status_change(client, payload, entity_type):
    """Normalize an explicitly requested status; never select a close state for the agent.

    Plain field/attachment edits need no workflow reads. Tasks use the separate
    task API; they must not inherit Story workflow rules.
    """
    if "status" not in payload and "v_status" not in payload:
        return
    if entity_type == "tasks":
        return
    if entity_type not in {"stories", "bug"}:
        raise WorkflowValidationError("不支持的工作项类型", entity_type=entity_type)
    workspace_id = payload.get("workspace_id")
    if not workspace_id:
        raise WorkflowValidationError("状态写入必须指定项目")
    system = "story" if entity_type == "stories" else "bug"
    scope = {"workspace_id": workspace_id, "system": system}
    current = None
    if "id" in payload:
        ticket_id = str(payload["id"]).strip()
        if not ticket_id.isdigit():
            raise WorkflowValidationError("状态更新必须指定一个有效票据 ID")
        query = {
            "workspace_id": workspace_id,
            "id": ticket_id,
            "limit": 2,
            "fields": "id,status,workspace_id",
        }
        if system == "story":
            query["entity_type"] = "stories"
            query["fields"] += ",workitem_type_id"
        data = _read(client, "get_stories" if system == "story" else "get_bug", query)
        key = "Story" if system == "story" else "Bug"
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
            or not isinstance(data[0].get(key), dict)
        ):
            raise WorkflowValidationError("未找到唯一票据，未执行状态更新")
        current = data[0][key]
        actual_id = str(current.get("id", ""))
        same_id = actual_id == ticket_id or (
            len(ticket_id) <= 9 and actual_id.endswith(ticket_id.zfill(9))
        )
        if not same_id or str(current.get("workspace_id")) != str(workspace_id):
            raise WorkflowValidationError("查询结果与目标票据或项目不一致")
    if system == "story":
        if payload.get("workitem_type_name") and not payload.get("workitem_type_id"):
            raise WorkflowValidationError(
                "同时设置状态和需求类别时必须提供 workitem_type_id"
            )
        type_id = payload.get("workitem_type_id") or (current or {}).get(
            "workitem_type_id"
        )
        if not type_id:
            raise WorkflowValidationError(
                "无法确定需求类别，不能使用项目中多个工作流的混合状态"
            )
        scope["workitem_type_id"] = type_id

    statuses = _state_map(
        _read(client, "get_workflows_status_map", scope), "status_map"
    )
    terminals = _state_map(
        _read(client, "get_workflows_last_steps", scope), "last_steps"
    )
    if not set(terminals).issubset(statuses):
        raise WorkflowValidationError("结束状态与合法状态映射不一致", scope=scope)
    details = {
        "scope": scope,
        "valid_statuses": statuses,
        "terminal_statuses": terminals,
    }
    target = payload.get("status")
    if "status" in payload and (not isinstance(target, str) or target not in statuses):
        raise WorkflowValidationError(
            "目标 status 不在当前工作流的合法状态中；不能将流转边界节点当作业务状态",
            **details,
        )
    if "v_status" in payload:
        alias = payload["v_status"]
        matches = (
            [key for key, label in statuses.items() if label == alias]
            if isinstance(alias, str)
            else []
        )
        if len(matches) != 1:
            raise WorkflowValidationError(
                "状态别名不存在或不唯一，请使用返回的合法 status", **details
            )
        if target is not None and target != matches[0]:
            raise WorkflowValidationError("status 与 v_status 指向不同状态", **details)
        target = matches[0]

    if current is not None and target != current.get("status"):
        transitions = _read(client, "get_workflows_all_transitions", scope)
        if not isinstance(transitions, list) or any(
            not isinstance(t, dict)
            or not isinstance(t.get("StepPrevious"), str)
            or not isinstance(t.get("StepNext"), str)
            for t in transitions
        ):
            raise WorkflowValidationError(
                "工作流流转配置格式不支持，未执行更新", **details
            )
        current_status = current.get("status")
        allowed = sorted(
            {
                t["StepNext"]
                for t in transitions
                if t["StepPrevious"] == current_status and t["StepNext"] in statuses
            }
        )
        if current_status not in statuses:
            raise WorkflowValidationError(
                "票据当前已处于非法状态，需要核实并修复，不能按正常流程自动流转",
                current_status=current_status,
                **details,
            )
        # Category changes can select a different workflow; do not infer an edge
        # from the old workflow or automatically advance through intermediate states.
        if system == "story" and str(scope["workitem_type_id"]) != str(
            current.get("workitem_type_id")
        ):
            raise WorkflowValidationError(
                "请将需求类别变更与状态变更分开执行，避免跨工作流推断路径", **details
            )
        if target not in allowed:
            raise WorkflowValidationError(
                "当前状态不能直接流转到目标状态，不会自动跳过中间步骤",
                current_status=current_status,
                next_statuses={k: statuses[k] for k in allowed},
                **details,
            )
    payload["status"] = target
    payload.pop("v_status", None)

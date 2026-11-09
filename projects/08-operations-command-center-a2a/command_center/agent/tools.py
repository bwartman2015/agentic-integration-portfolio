"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def get_case(case_id):
    """Read an existing dispute case and prior findings. TODO(M3)."""
    raise NotImplementedError("get_case")


def record_resolution(case_id, recommendation, findings):
    """Write a resolution package (findings per agent, recommendation, evidence links) for approval. TODO(M3)."""
    raise NotImplementedError("record_resolution")


TOOLS = [
    Tool('get_case', 'Read an existing dispute case and prior findings', handler=get_case,
         params={'case_id': 'string'}, required=['case_id'],
         scope='cases.read', side_effect='read', category='database'),
    Tool('record_resolution', 'Write a resolution package (findings per agent, recommendation, evidence links) for approval', handler=record_resolution,
         params={'case_id': 'string', 'recommendation': 'string', 'findings': 'array'}, required=['case_id', 'recommendation', 'findings'],
         scope='cases.write', side_effect='write', category='database'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg

"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def read_file(path):
    """Read a repository file. TODO(M3)."""
    raise NotImplementedError("read_file")


def get_spec_diff():
    """OpenAPI diff for this PR. TODO(M3)."""
    raise NotImplementedError("get_spec_diff")


def write_test_file(path, content):
    """Write a generated test under tests/ai_generated/. TODO(M3)."""
    raise NotImplementedError("write_test_file")


def run_tests():
    """Run pytest + Schemathesis; returns pass/fail and coverage. TODO(M3)."""
    raise NotImplementedError("run_tests")


def post_review_comment(path, line, body):
    """Post an advisory review comment on the PR. TODO(M4)."""
    raise NotImplementedError("post_review_comment")


TOOLS = [
    Tool('read_file', 'Read a repository file', handler=read_file,
         params={'path': 'string'}, required=['path'],
         scope='repo.read', side_effect='read', category='file'),
    Tool('get_spec_diff', 'OpenAPI diff for this PR', handler=get_spec_diff,
         params={}, required=[],
         scope='repo.read', side_effect='read', category='file'),
    Tool('write_test_file', 'Write a generated test under tests/ai_generated/', handler=write_test_file,
         params={'path': 'string', 'content': 'string'}, required=['path', 'content'],
         scope='repo.tests', side_effect='write', category='file'),
    Tool('run_tests', 'Run pytest + Schemathesis; returns pass/fail and coverage', handler=run_tests,
         params={}, required=[],
         scope='repo.read', side_effect='read', category='code'),
    Tool('post_review_comment', 'Post an advisory review comment on the PR', handler=post_review_comment,
         params={'path': 'string', 'line': 'integer', 'body': 'string'}, required=['path', 'line', 'body'],
         scope='repo.review', side_effect='write', category='external'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg

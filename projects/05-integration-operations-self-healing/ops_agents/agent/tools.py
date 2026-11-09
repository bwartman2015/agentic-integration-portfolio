"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def get_traces(service, minutes=None):
    """Recent error traces for a service (Tempo API). TODO(M3)."""
    raise NotImplementedError("get_traces")


def sample_dlq(queue, n=None):
    """Peek N messages from a DLQ. TODO(M3)."""
    raise NotImplementedError("sample_dlq")


def get_metrics(service):
    """Current vs baseline error rate, throughput and DLQ depth (Prometheus). TODO(M3)."""
    raise NotImplementedError("get_metrics")


def replay_dlq(queue, max_batch):
    """Replay up to max_batch messages from a DLQ. TODO(M3)."""
    raise NotImplementedError("replay_dlq")


def pause_consumer(service):
    """Pause a consumer. TODO(M3)."""
    raise NotImplementedError("pause_consumer")


def set_rate_limit(service, per_second):
    """Throttle a consumer (msgs/sec). TODO(M3)."""
    raise NotImplementedError("set_rate_limit")


def quarantine(queue, message_ids):
    """Move poison/unmappable messages to a quarantine queue. TODO(M3)."""
    raise NotImplementedError("quarantine")


def open_fix_pr(service, evidence):
    """Ask Claude Code (headless) to draft a fix + test and open a PR. TODO(M4)."""
    raise NotImplementedError("open_fix_pr")


def notify_oncall(severity, summary):
    """Page the on-call human with an incident summary. TODO(M3)."""
    raise NotImplementedError("notify_oncall")


TOOLS = [
    Tool('get_traces', 'Recent error traces for a service (Tempo API)', handler=get_traces,
         params={'service': 'string', 'minutes': 'integer'}, required=['service'],
         scope='ops.read', side_effect='read', category='external'),
    Tool('sample_dlq', 'Peek N messages from a DLQ', handler=sample_dlq,
         params={'queue': 'string', 'n': 'integer'}, required=['queue'],
         scope='ops.read', side_effect='read', category='api'),
    Tool('get_metrics', 'Current vs baseline error rate, throughput and DLQ depth (Prometheus)', handler=get_metrics,
         params={'service': 'string'}, required=['service'],
         scope='ops.read', side_effect='read', category='external'),
    Tool('replay_dlq', 'Replay up to max_batch messages from a DLQ', handler=replay_dlq,
         params={'queue': 'string', 'max_batch': 'integer'}, required=['queue', 'max_batch'],
         scope='ops.remediate', side_effect='write', category='api'),
    Tool('pause_consumer', 'Pause a consumer', handler=pause_consumer,
         params={'service': 'string'}, required=['service'],
         scope='ops.remediate', side_effect='write', category='api'),
    Tool('set_rate_limit', 'Throttle a consumer (msgs/sec)', handler=set_rate_limit,
         params={'service': 'string', 'per_second': 'integer'}, required=['service', 'per_second'],
         scope='ops.remediate', side_effect='write', category='api'),
    Tool('quarantine', 'Move poison/unmappable messages to a quarantine queue', handler=quarantine,
         params={'queue': 'string', 'message_ids': 'array'}, required=['queue', 'message_ids'],
         scope='ops.remediate', side_effect='write', category='api'),
    Tool('open_fix_pr', 'Ask Claude Code (headless) to draft a fix + test and open a PR', handler=open_fix_pr,
         params={'service': 'string', 'evidence': 'string'}, required=['service', 'evidence'],
         scope='ops.code', side_effect='write', category='code'),
    Tool('notify_oncall', 'Page the on-call human with an incident summary', handler=notify_oncall,
         params={'severity': 'string', 'summary': 'string'}, required=['severity', 'summary'],
         scope='ops.notify', side_effect='write', category='external'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg

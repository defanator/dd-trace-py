import os
import platform
import logging
import importlib
from ddtrace import __version__


DEFAULT_AGENT_HOST = '127.0.0.1'
DEFAULT_METRIC_AGENT_PORT = 8125

# Default tags to apply to metrics
DEFAULT_ENABLED_TAGS = set([
    'datadog.tracer.lang',
    'datadog.tracer.lang_interpreter',
    'datadog.tracer.lang_version',
    'datadog.tracer.version',
])

# Default metrics to collect
DEFAULT_ENABLED_METRICS = set([
    'datadog.tracer.runtime.thread_count',
    'datadog.tracer.runtime.mem.rss',
    'datadog.tracer.runtime.gc.gen1_count',
    'datadog.tracer.runtime.gc.gen2_count',
    'datadog.tracer.runtime.gc.gen3_count',
])


log = logging.getLogger(__name__)


def process_id():
    """Returns the current process id of this python process.
    """
    return os.getpid()


def interpreter_implementation(modules, keys):
    """Returns the Python interpreter implementation.

    For CPython this is 'CPython'.
    For Pypy this is 'PyPy'.
    For Jython this is 'Jython'.
    """
    return {
        'datadog.tracer.lang_interpreter': platform.python_implementation()
    }


def interpreter_version(modules, keys):
    """Returns the interpreter version as a string.

    >>>> interpreter_version()
    '2.7.13'
    """
    if 'datadog.tracer.lang_version' not in keys:
        return {}
    return {
        'datadog.tracer.lang_version': platform.python_version()
    }


def tracer_version(modules, keys):
    """Returns the ddtrace version."""
    if 'datadog.tracer.version' not in keys:
        return {}
    return {
        'datadog.tracer.version': __version__
    }


def gc_count(modules, keys):
    """Returns the gc count of the collections of the first 3 generations.
    More information:
        - https://docs.python.org/3/library/gc.html
    """
    gc = modules.get('gc')
    metrics = {}

    # DEV: short-cut to avoid the get_count() call if none of the metrics are
    #      enabled.
    if set([
        'datadog.tracer.runtime.gc.gen1_count',
        'datadog.tracer.runtime.gc.gen2_count',
        'datadog.tracer.runtime.gc.gen3_count',
    ]).intersection(keys) == set():
        return {}

    count = gc.get_count()
    if 'datadog.tracer.runtime.gc.gen1_count' in keys:
        metrics['datadog.tracer.runtime.gc.gen1_count'] = count[0]
    if 'datadog.tracer.runtime.gc.gen2_count' in keys:
        metrics['datadog.tracer.runtime.gc.gen2_count'] = count[1]
    if 'datadog.tracer.runtime.gc.gen3_count' in keys:
        metrics['datadog.tracer.runtime.gc.gen3_count'] = count[2]

    return metrics


class MetricCollector(object):
    """A basic state machine useful for collecting, caching and updating data
    obtained from different Python modules.

    The two primary use-cases are
    1) data loaded once (like tagging information)
    2) periodically updating data sources (like thread count)

    Functionality is provided for requiring modules which may or may not be
    installed.
    """
    def __init__(self, collect_fn=None, enabled=True, periodic=False, required_modules=None):
        self._collect_fn = collect_fn
        self.enabled = enabled
        self.periodic = periodic
        self.required_modules = required_modules or []

        self.metrics = {}  # the metrics to be collected and cached
        self.metrics_loaded = False  # whether or not the metrics have been loaded
        self.modules = self._load_modules()

    def _load_modules(self):
        modules = {}
        try:
            for module in self.required_modules:
                modules[module] = importlib.import_module(module)
        except ImportError:
            # DEV: disable collector if we cannot load any of the required modules
            self.enabled = False
            log.warn('Could not import module "{}" for {}'.format(module, self))
            return None
        return modules

    def collect_fn(self, modules, keys):
        """Returns metrics given a set of keys and provided modules.

        Note: this method has to be provided as an argument to the intializer
        or overridden by a child class.

        :param modules: modules loaded from `required_modules`
        :param keys: set of keys to collect
        :return: collected metrics as a dict
        """
        if not self._collect_fn:
            raise NotImplementedError('A collect function must be implemented')
        return self._collect_fn(modules, keys)

    def collect(self, keys=None):
        """Returns metrics as collected by `collect_fn`.

        :param keys: The keys of the metrics to collect.
        """
        if not self.enabled:
            return self.metrics

        keys = keys or set()

        if not self.periodic and self.metrics_loaded:
            return self.metrics

        self.metrics = self.collect_fn(self.modules, keys)
        self.metrics_loaded = True
        return self.metrics

    def __repr__(self):
        return '<Collector(enabled={},periodic={},required_modules={})>'.format(
            self.enabled,
            self.periodic,
            self.required_modules
        )


class PSUtilRuntimeMetricCollector(MetricCollector):
    """Collector for psutil metrics.

    Performs batched operations via proc.oneshot() to optimize the calls.
    See https://psutil.readthedocs.io/en/latest/#psutil.Process.oneshot
    for more information.
    """
    def __init__(self, *args, **kwargs):
        kwargs['required_modules'] = ['psutil']
        self.proc = None
        super(PSUtilRuntimeMetricCollector, self).__init__(*args, **kwargs)

    def collect_fn(self, modules, keys):
        if not self.proc:
            self.proc = self.modules['psutil'].Process(os.getpid())

        metrics = {}
        with self.proc.oneshot():
            if 'datadog.tracer.runtime.thread_count' in keys:
                metrics['datadog.tracer.runtime.thread_count'] = self.proc.num_threads()
            if 'datadog.tracer.runtime.mem.rss' in keys:
                metrics['datadog.tracer.runtime.mem.rss'] = self.proc.memory_info().rss
        return metrics


class RuntimeMetricTagCollector(MetricCollector):
    pass


class RuntimeMetricCollector(MetricCollector):
    pass


class RuntimeMetricsCollector(object):
    """
    TODO: configuration
    TODO: support for forking/multi-process app (multiple pids)
    """

    METRIC_COLLECTORS = [
        PSUtilRuntimeMetricCollector(periodic=True),
        RuntimeMetricCollector(
            collect_fn=gc_count,
            periodic=True,
            required_modules=['gc'],
        )
    ]

    TAG_COLLECTORS = [
        RuntimeMetricTagCollector(collect_fn=interpreter_version),
        RuntimeMetricTagCollector(collect_fn=interpreter_implementation),
        RuntimeMetricTagCollector(collect_fn=tracer_version),
    ]

    def __init__(self, enabled_metrics=DEFAULT_ENABLED_METRICS, enabled_tags=DEFAULT_ENABLED_TAGS):
        self.agent_host = DEFAULT_AGENT_HOST
        self.agent_metric_port = DEFAULT_METRIC_AGENT_PORT
        self.enabled_metrics = enabled_metrics
        self.enabled_tags = enabled_tags
        self.statsd = None

        self._init_statsd()

    def _collect_constant_tags(self):
        """Collects tags to be sent to ddstatsd.

        Note: ddstatsd expects tags in the form ['key1:value1', 'key2:value2', ...]
        :return:
        """
        tags = []
        for tag_collector in self.TAG_COLLECTORS:
            collected_tags = tag_collector.collect(self.enabled_tags)
            tags += ['{}:{}'.format(k, v) for k, v in collected_tags.items()]
        log.info('Reporting constant tags {}'.format(tags))
        return tags

    def _collect_metrics(self):
        metrics = {}
        for metric_collector in self.METRIC_COLLECTORS:
            collector_metrics = metric_collector.collect(self.enabled_metrics)
            metrics.update(collector_metrics)
        return metrics

    def _init_statsd(self):
        try:
            from datadog import DogStatsd
            tags = self._collect_constant_tags()
            self.statsd = DogStatsd(host=self.agent_host, port=self.agent_metric_port, constant_tags=tags)
        except ImportError:
            log.info('Install the `datadog` package to enable runtime metrics.')
        except Exception:
            log.warn('Could not initialize ddstatsd')

    def flush(self):
        """Collects and flushes enabled metrics to the Datadog Agent.
        """
        if not self.statsd:
            log.warn('Attempted flush with uninitialized or failed statsd client')
            return

        metrics = self._collect_metrics()

        for metric_key, metric_value in metrics.items():
            log.info('Flushing metric "{}:{}" to Datadog agent'.format(metric_key, metric_value))
            self.statsd.gauge(metric_key, metric_value)
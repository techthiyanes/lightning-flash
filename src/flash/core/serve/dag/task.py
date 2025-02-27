from collections import defaultdict
from typing import List, Sequence

from flash.core.utilities.imports import _TOPIC_SERVE_AVAILABLE

# Skip doctests if requirements aren't available
if not _TOPIC_SERVE_AVAILABLE:
    __doctest_skip__ = ["*"]

no_default = "__no_default__"


def ishashable(x):
    """Is x hashable?

    Examples
    --------
    >>> ishashable(1)
    True
    >>> ishashable([1])
    False

    """
    try:
        hash(x)
        return True
    except TypeError:
        return False


def istask(x):
    """Is x a runnable task? A task is a tuple with a callable first argument.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> istask((inc, 1))
    True
    >>> istask(1)
    False

    """
    return type(x) is tuple and x and callable(x[0])


def preorder_traversal(task):
    """A generator to preorder-traverse a task."""

    for item in task:
        if istask(item):
            yield from preorder_traversal(item)
        elif isinstance(item, list):
            yield list
            yield from preorder_traversal(item)
        else:
            yield item


def lists_to_tuples(res, keys):
    if isinstance(keys, list):
        return tuple(lists_to_tuples(r, k) for r, k in zip(res, keys))
    return res


def _execute_task(arg, cache):
    """Do the actual work of collecting data and executing a function.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc
    >>> cache = {'x': 1, 'y': 2}  # Compute tasks against a cache
    >>> _execute_task((add, 'x', 1), cache)  # Compute task in naive manner
    2
    >>> _execute_task((add, (inc, 'x'), 1), cache)  # Support nested computation
    3
    >>> _execute_task('x', cache)  # Also grab data from cache
    1
    >>> list(_execute_task(['x', 'y'], cache))  # Support nested lists
    [1, 2]
    >>> list(map(list, _execute_task([['x', 'y'], ['y', 'x']], cache)))
    [[1, 2], [2, 1]]
    >>> _execute_task('foo', cache)  # Passes through on non-keys
    'foo'

    """
    if isinstance(arg, list):
        return [_execute_task(a, cache) for a in arg]
    if istask(arg):
        func, args = arg[0], arg[1:]
        # Note: Don't assign the subtask results to a variable. numpy detects
        # temporaries by their reference count and can execute certain
        # operations in-place.
        return func(*(_execute_task(a, cache) for a in args))
    if not ishashable(arg):
        return arg
    if arg in cache:
        return cache[arg]
    return arg


def get(dsk: dict, out: Sequence[str], cache: dict = None, sortkeys: List[str] = None):
    """Get value from the task graphs.

    Parameters
    ----------
    dsk
        task graph dict
    out
        sequence of output keys which should be retrieved as results of running
        `get()` over the `dsk`.
    cache
        cache dict for fast in-memory lookups of previously computed results.
    sortkeys
        topologically sorted keys

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> d = {'x': 1, 'y': (inc, 'x')}
    >>> get(d, 'x')
    1
    >>> get(d, 'y')
    2
    >>> get(d, 'y', sortkeys=['x', 'y'])
    2

    """
    for k in flatten(out) if isinstance(out, list) else [out]:
        if k not in dsk:
            raise KeyError(f"{k} is not a key in the graph")
    if cache is None:
        cache = {}
    if sortkeys is None:
        sortkeys = toposort(dsk)
    for key in sortkeys:
        task = dsk[key]
        result = _execute_task(task, cache)
        cache[key] = result
    result = _execute_task(out, cache)
    if isinstance(out, list):
        result = lists_to_tuples(result, out)
    return result


def get_dependencies(dsk, key=None, task=no_default, as_list=False):
    """Get the immediate tasks on which this task depends.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc
    >>> dsk = {'x': 1,
    ...        'y': (inc, 'x'),
    ...        'z': (add, 'x', 'y'),
    ...        'w': (inc, 'z'),
    ...        'a': (add, (inc, 'x'), 1)}
    >>> get_dependencies(dsk, 'x')
    set()
    >>> get_dependencies(dsk, 'y')
    {'x'}
    >>> sorted(get_dependencies(dsk, 'z'))
    ['x', 'y']
    >>> get_dependencies(dsk, 'w')  # Only direct dependencies
    {'z'}
    >>> get_dependencies(dsk, 'a')  # Ignore non-keys
    {'x'}
    >>> get_dependencies(dsk, task=(inc, 'x'))  # provide tasks directly
    {'x'}

    """
    if key is not None:
        arg = dsk[key]
    elif task is not no_default:
        arg = task
    else:
        raise ValueError("Provide either key or task")

    result = []
    work = [arg]

    while work:
        new_work = []
        for w in work:
            typ = type(w)
            if typ is tuple and w and callable(w[0]):  # istask(w)
                new_work.extend(w[1:])
            elif typ is list:
                new_work.extend(w)
            elif typ is dict:
                new_work.extend(w.values())
            else:
                try:
                    if w in dsk:
                        result.append(w)
                except TypeError:  # not hashable
                    pass
        work = new_work

    return result if as_list else set(result)


def get_deps(dsk):
    """Get dependencies and dependents from task graph.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b')}
    >>> dependencies, dependents = get_deps(dsk)
    >>> dependencies
    {'a': set(), 'b': {'a'}, 'c': {'b'}}
    >>> dict(dependents)
    {'a': {'b'}, 'b': {'c'}, 'c': set()}

    """
    dependencies = {k: get_dependencies(dsk, task=v) for k, v in dsk.items()}
    dependents = reverse_dict(dependencies)
    return dependencies, dependents


def flatten(seq, container=list):
    """
    >>> list(flatten([1]))
    [1]
    >>> list(flatten([[1, 2], [1, 2]]))
    [1, 2, 1, 2]
    >>> list(flatten([[[1], [2]], [[1], [2]]]))
    [1, 2, 1, 2]
    >>> list(flatten(((1, 2), (1, 2)))) # Don't flatten tuples
    [(1, 2), (1, 2)]
    >>> list(flatten((1, 2, [3, 4]))) # support heterogeneous
    [1, 2, 3, 4]
    """
    if isinstance(seq, str):
        yield seq
    else:
        for item in seq:
            if isinstance(item, container):
                yield from flatten(item, container=container)
            else:
                yield item


def reverse_dict(d):
    """
    >>> a, b, c = 'abc'
    >>> d = {a: [b, c], b: [c]}
    >>> dd = reverse_dict(d)
    >>> from pprint import pprint
    >>> pprint({k: sorted(v) for k, v in dd.items()})
    {'a': [], 'b': ['a'], 'c': ['a', 'b']}
    """
    result = defaultdict(set)
    _add = set.add
    for k, vals in d.items():
        result[k]
        for val in vals:
            _add(result[val], k)
    result.default_factory = None
    return result


def subs(task, key, val):
    """Perform a substitution on a task.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> subs((inc, 'x'), 'x', 1)  # doctest: +ELLIPSIS
    (<function inc at ...>, 1)

    """
    type_task = type(task)
    if not (type_task is tuple and task and callable(task[0])):  # istask(task):
        try:
            if type_task is type(key) and task == key:
                return val
        except Exception:
            pass
        if type_task is list:
            return [subs(x, key, val) for x in task]
        return task
    newargs = []
    for arg in task[1:]:
        type_arg = type(arg)
        if type_arg is tuple and arg and callable(arg[0]):  # istask(task):
            arg = subs(arg, key, val)
        elif type_arg is list:
            arg = [subs(x, key, val) for x in arg]
        elif type_arg is type(key):
            try:
                # Can't do a simple equality check, since this may trigger
                # a FutureWarning from NumPy about array equality
                # https://github.com/dask/dask/pull/2457
                if len(arg) == len(key) and all(type(aa) is type(bb) and aa == bb for aa, bb in zip(arg, key)):
                    arg = val

            except (TypeError, AttributeError):
                # Handle keys which are not sized (len() fails), but are hashable
                if arg == key:
                    arg = val
        newargs.append(arg)
    return task[:1] + tuple(newargs)


def _toposort(dsk, keys=None, returncycle=False, dependencies=None):
    """Stack-based depth-first search traversal.

    This is based on Tarjan's method for topological sorting (see wikipedia for pseudocode).

    """
    if keys is None:
        keys = dsk
    elif not isinstance(keys, list):
        keys = [keys]
    if not returncycle:
        ordered = []

    # Nodes whose descendents have been completely explored.
    # These nodes are guaranteed to not be part of a cycle.
    completed = set()

    # All nodes that have been visited in the current traversal.  Because
    # we are doing depth-first search, going "deeper" should never result
    # in visiting a node that has already been seen.  The `seen` and
    # `completed` sets are mutually exclusive; it is okay to visit a node
    # that has already been added to `completed`.
    seen = set()

    if dependencies is None:
        dependencies = {k: get_dependencies(dsk, k) for k in dsk}

    for key in keys:
        if key in completed:
            continue
        nodes = [key]
        while nodes:
            # Keep current node on the stack until all descendants are visited
            cur = nodes[-1]
            if cur in completed:
                # Already fully traversed descendants of cur
                nodes.pop()
                continue
            seen.add(cur)

            # Add direct descendants of cur to nodes stack
            next_nodes = []
            for nxt in dependencies[cur]:
                if nxt not in completed:
                    if nxt in seen:
                        # Cycle detected!
                        cycle = [nxt]
                        while nodes[-1] != nxt:
                            cycle.append(nodes.pop())
                        cycle.append(nodes.pop())
                        cycle.reverse()
                        if returncycle:
                            return cycle
                        cycle = "->".join(str(x) for x in cycle)
                        raise RuntimeError("Cycle detected in task graph: %s" % cycle)
                    next_nodes.append(nxt)

            if next_nodes:
                nodes.extend(next_nodes)
            else:
                # cur has no more descendants to explore, so we're done with it
                if not returncycle:
                    ordered.append(cur)
                completed.add(cur)
                seen.remove(cur)
                nodes.pop()
    if returncycle:
        return []
    return ordered


def toposort(dsk, dependencies=None):
    """Return a list of keys of task graph sorted in topological order."""
    return _toposort(dsk, dependencies=dependencies)


def getcycle(d, keys):
    """Return a list of nodes that form a cycle if graph is not a DAG. Returns an empty list if no cycle is found.
    ``keys`` may be a single key or list of keys.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> d = {'x': (inc, 'z'), 'y': (inc, 'x'), 'z': (inc, 'y')}
    >>> getcycle(d, 'x')
    ['x', 'z', 'y', 'x']

    See Also
    --------
    isdag
    """
    return _toposort(d, keys=keys, returncycle=True)


def isdag(d, keys):
    """Does graph form a directed acyclic graph when calculating keys? ``keys`` may be a single key or list of keys.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> isdag({'x': 0, 'y': (inc, 'x')}, 'y')
    True
    >>> isdag({'x': (inc, 'y'), 'y': (inc, 'x')}, 'y')
    False

    See Also
    --------
    getcycle
    """
    return not getcycle(d, keys)


class literal:
    """A small serializable object to wrap literal values without copying."""

    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data

    def __repr__(self):
        return "literal<type=%s>" % type(self.data).__name__

    def __reduce__(self):
        return literal, (self.data,)

    def __call__(self):
        return self.data


def quote(x):
    """Ensure that this value remains this value in a task graph Some values in task graph take on special meaning.
    Sometimes we want to ensure that our data is not interpreted but remains literal.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add
    >>> quote((add, 1, 2))
    (literal<type=tuple>,)

    """
    if istask(x) or type(x) is list or type(x) is dict:
        return (literal(x),)
    return x

import math
import numbers
from enum import Enum

from flash.core.serve.dag.task import flatten, get, get_dependencies, ishashable, istask, reverse_dict, subs, toposort
from flash.core.serve.dag.utils import key_split
from flash.core.utilities.imports import _TOPIC_SERVE_AVAILABLE

# Skip doctests if requirements aren't available
if not _TOPIC_SERVE_AVAILABLE:
    __doctest_skip__ = ["*"]


def cull(dsk, keys):
    """Return new task graph with only the tasks required to calculate keys.

    In other words, remove unnecessary tasks from task graph.
    ``keys`` may be a single key or list of keys.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc
    >>> d = {'x': 1, 'y': (inc, 'x'), 'out': (add, 'x', 10)}
    >>> dsk, dependencies = cull(d, 'out')
    >>> dsk  # doctest: +ELLIPSIS
    {'out': (<function add at ...>, 'x', 10), 'x': 1}
    >>> dependencies
    {'out': ['x'], 'x': []}

    Returns
    -------
    dsk: culled graph
    dependencies: Dict mapping {key: [deps]}.  Useful side effect to accelerate
        other optimizations, notably fuse.

    """
    if not isinstance(keys, (list, set)):
        keys = [keys]

    seen = set()
    dependencies = {}
    out = {}
    work = list(set(flatten(keys)))

    while work:
        new_work = []
        for k in work:
            dependencies_k = get_dependencies(dsk, k, as_list=True)  # fuse needs lists
            out[k] = dsk[k]
            dependencies[k] = dependencies_k
            for d in dependencies_k:
                if d not in seen:
                    seen.add(d)
                    new_work.append(d)

        work = new_work

    return out, dependencies


def default_fused_linear_keys_renamer(keys):
    """Create new keys for fused tasks."""
    typ = type(keys[0])
    if typ is str:
        names = [key_split(x) for x in keys[:0:-1]]
        names.append(keys[0])
        return "-".join(names)
    if typ is tuple and len(keys[0]) > 0 and isinstance(keys[0][0], str):
        names = [key_split(x) for x in keys[:0:-1]]
        names.append(keys[0][0])
        return ("-".join(names),) + keys[0][1:]
    return None


def fuse_linear(dsk, keys=None, dependencies=None, rename_keys=True):
    """Return new dask graph with linear sequence of tasks fused together.

    If specified, the keys in ``keys`` keyword argument are *not* fused.
    Supply ``dependencies`` from output of ``cull`` if available to avoid
    recomputing dependencies.

    **This function is mostly superseded by ``fuse``**

    Parameters
    ----------
    dsk: dict
    keys: list
    dependencies: dict, optional
        {key: [list-of-keys]}.  Must be a list to provide count of each key
        This optional input often comes from ``cull``
    rename_keys: bool or func, optional
        Whether to rename fused keys with ``default_fused_linear_keys_renamer``
        or not.  Renaming fused keys can keep the graph more understandable
        and comprehensive, but it comes at the cost of additional processing.
        If False, then the top-most key will be used.  For advanced usage, a
        func is also accepted, ``new_key = rename_keys(fused_key_list)``.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import inc
    >>> d = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b')}
    >>> dsk, dependencies = fuse(d)
    >>> dsk  # doctest: +ELLIPSIS
    {'c': 'a-b-c', 'a-b-c': (<function inc at ...>, (<function inc at ...>, 1))}
    >>> dsk, dependencies = fuse(d, rename_keys=False)
    >>> dsk  # doctest: +ELLIPSIS
    {'c': (<function inc at ...>, (<function inc at ...>, 1))}
    >>> dsk, dependencies = fuse(d, keys=['b'], rename_keys=False)
    >>> dsk  # doctest: +ELLIPSIS
    {'b': (<function inc at ...>, 1), 'c': (<function inc at ...>, 'b')}

    Returns
    -------
    dsk: output graph with keys fused
    dependencies: dict mapping dependencies after fusion.  Useful side effect
        to accelerate other downstream optimizations.

    """
    if keys is not None and not isinstance(keys, set):
        if not isinstance(keys, list):
            keys = [keys]
        keys = set(flatten(keys))

    if dependencies is None:
        dependencies = {k: get_dependencies(dsk, k, as_list=True) for k in dsk}

    # locate all members of linear chains
    child2parent = {}
    unfusible = set()
    for parent in dsk:
        deps = dependencies[parent]
        has_many_children = len(deps) > 1
        for child in deps:
            if keys is not None and child in keys:
                unfusible.add(child)
            elif child in child2parent:
                del child2parent[child]
                unfusible.add(child)
            elif has_many_children:
                unfusible.add(child)
            elif child not in unfusible:
                child2parent[child] = parent

    # construct the chains from ancestor to descendant
    chains = []
    parent2child = dict(map(reversed, child2parent.items()))
    while child2parent:
        child, parent = child2parent.popitem()
        chain = [child, parent]
        while parent in child2parent:
            parent = child2parent.pop(parent)
            del parent2child[parent]
            chain.append(parent)
        chain.reverse()
        while child in parent2child:
            child = parent2child.pop(child)
            del child2parent[child]
            chain.append(child)
        chains.append(chain)

    dependencies = {k: set(v) for k, v in dependencies.items()}

    if rename_keys is True:
        key_renamer = default_fused_linear_keys_renamer
    elif rename_keys is False:
        key_renamer = None
    else:
        key_renamer = rename_keys

    # create a new dask with fused chains
    rv = {}
    fused = set()
    aliases = set()
    is_renamed = False
    for chain in chains:
        if key_renamer is not None:
            new_key = key_renamer(chain)
            is_renamed = new_key is not None and new_key not in dsk and new_key not in rv
        child = chain.pop()
        val = dsk[child]
        while chain:
            parent = chain.pop()
            dependencies[parent].update(dependencies.pop(child))
            dependencies[parent].remove(child)
            val = subs(dsk[parent], child, val)
            fused.add(child)
            child = parent
        fused.add(child)
        if is_renamed:
            rv[new_key] = val
            rv[child] = new_key
            dependencies[new_key] = dependencies[child]
            dependencies[child] = {new_key}
            aliases.add(child)
        else:
            rv[child] = val
    for key, val in dsk.items():
        if key not in fused:
            rv[key] = val
    if aliases:
        for key, deps in dependencies.items():
            for old_key in deps & aliases:
                new_key = rv[old_key]
                deps.remove(old_key)
                deps.add(new_key)
                rv[key] = subs(rv[key], old_key, new_key)
        if keys is not None:
            for key in aliases - keys:
                del rv[key]
                del dependencies[key]
    return rv, dependencies


def _flat_set(x):
    if x is None:
        return set()
    if isinstance(x, set):
        return x
    if not isinstance(x, (list, set)):
        x = [x]
    return set(x)


def inline(dsk, keys=None, inline_constants=True, dependencies=None):
    """Return new dask with the given keys inlined with their values.

    Inlines all constants if ``inline_constants`` keyword is True. Note that
    the constant keys will remain in the graph, to remove them follow
    ``inline`` with ``cull``.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc
    >>> d = {'x': 1, 'y': (inc, 'x'), 'z': (add, 'x', 'y')}
    >>> inline(d)  # doctest: +ELLIPSIS
    {'x': 1, 'y': (<function inc at ...>, 1), 'z': (<function add at ...>, 1, 'y')}
    >>> inline(d, keys='y')  # doctest: +ELLIPSIS
    {'x': 1, 'y': (<function inc at ...>, 1), 'z': (<function add at ...>, 1, (<function inc at ...>, 1))}
    >>> inline(d, keys='y', inline_constants=False)  # doctest: +ELLIPSIS
    {'x': 1, 'y': (<function inc at ...>, 'x'), 'z': (<function add at ...>, 'x', (<function inc at ...>, 'x'))}

    """
    if dependencies and isinstance(next(iter(dependencies.values())), list):
        dependencies = {k: set(v) for k, v in dependencies.items()}

    keys = _flat_set(keys)

    if dependencies is None:
        dependencies = {k: get_dependencies(dsk, k) for k in dsk}

    if inline_constants:
        keys.update(
            k for k, v in dsk.items() if (ishashable(v) and v in dsk) or (not dependencies[k] and not istask(v))
        )

    # Keys may depend on other keys, so determine replace order with toposort.
    # The values stored in `keysubs` do not include other keys.
    replaceorder = toposort({k: dsk[k] for k in keys if k in dsk}, dependencies=dependencies)
    keysubs = {}
    for key in replaceorder:
        val = dsk[key]
        for dep in keys & dependencies[key]:
            replace = keysubs.get(dep, dsk[dep])
            val = subs(val, dep, replace)
        keysubs[key] = val

    # Make new dask with substitutions
    dsk2 = keysubs.copy()
    for key, val in dsk.items():
        if key not in dsk2:
            for item in keys & dependencies[key]:
                val = subs(val, item, keysubs[item])
            dsk2[key] = val
    return dsk2


def inline_functions(dsk, output, fast_functions=None, inline_constants=False, dependencies=None):
    """Inline cheap functions into larger operations.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc
    >>> double = lambda x: x*2
    >>> dsk = {'out': (add, 'i', 'd'),
    ...        'i': (inc, 'x'),
    ...        'd': (double, 'y'),
    ...        'x': 1, 'y': 1}
    >>> inline_functions(dsk, [], [inc])  # doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    {'x': 1,
     'out': (<function add at ...>, (<function inc at ...>, 'x'), 'd'),
     'd': (<function <lambda> at ...>, 'y'),
     'y': 1}

    Protect output keys.  In the example below ``i`` is not inlined because it
    is marked as an output key.

    >>> inline_functions(dsk, ['i', 'out'], [inc, double])  # doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    {'y': 1,
     'out': (<function add at ...>, 'i', (<function <lambda> at ...>, 'y')),
     'i': (<function inc at ...>, 'x'),
     'x': 1}

    """
    if not fast_functions:
        return dsk

    output = set(output)

    fast_functions = set(fast_functions)

    if dependencies is None:
        dependencies = {k: get_dependencies(dsk, k) for k in dsk}
    dependents = reverse_dict(dependencies)

    def inlinable(v):
        try:
            return functions_of(v).issubset(fast_functions)
        except TypeError:
            return False

    keys = [k for k, v in dsk.items() if istask(v) and dependents[k] and k not in output and inlinable(v)]

    if keys:
        dsk = inline(dsk, keys, inline_constants=inline_constants, dependencies=dependencies)
        for k in keys:
            del dsk[k]
    return dsk


def unwrap_partial(func):
    while hasattr(func, "func"):
        func = func.func
    return func


def functions_of(task):
    """Set of functions contained within nested task.

    Examples
    --------
    >>> from flash.core.serve.dag.utils_test import add, inc, mul
    >>> task = (add, (mul, 1, 2), (inc, 3))
    >>> sorted(functions_of(task), key=str)  # doctest: +ELLIPSIS
    [<function add at ...>, <function inc at ...>, <function mul at ...>]

    """
    funcs = set()

    work = [task]
    sequence_types = {list, tuple}

    while work:
        new_work = []
        for task in work:
            if type(task) in sequence_types:
                if istask(task):
                    funcs.add(unwrap_partial(task[0]))
                    new_work.extend(task[1:])
                else:
                    new_work.extend(task)
        work = new_work

    return funcs


def default_fused_keys_renamer(keys, max_fused_key_length=120):
    """Create new keys for ``fuse`` tasks.

    The optional parameter `max_fused_key_length` is used to limit the maximum string length for each renamed key. If
    this parameter is set to `None`, there is no limit.
    """
    it = reversed(keys)
    first_key = next(it)
    typ = type(first_key)

    if max_fused_key_length:  # Take into account size of hash suffix
        max_fused_key_length -= 5

    def _enforce_max_key_limit(key_name):
        if max_fused_key_length and len(key_name) > max_fused_key_length:
            name_hash = f"{hash(key_name):x}"[:4]
            key_name = f"{key_name[:max_fused_key_length]}-{name_hash}"
        return key_name

    if typ is str:
        first_name = key_split(first_key)
        names = {key_split(k) for k in it}
        names.discard(first_name)
        names = sorted(names)
        names.append(first_key)
        concatenated_name = "-".join(names)
        return _enforce_max_key_limit(concatenated_name)
    if typ is tuple and len(first_key) > 0 and isinstance(first_key[0], str):
        first_name = key_split(first_key)
        names = {key_split(k) for k in it}
        names.discard(first_name)
        names = sorted(names)
        names.append(first_key[0])
        concatenated_name = "-".join(names)
        return (_enforce_max_key_limit(concatenated_name),) + first_key[1:]
    return None


# PEP-484 compliant singleton constant
# https://www.python.org/dev/peps/pep-0484/#support-for-singleton-types-in-unions
class Default(Enum):
    token = 0

    def __repr__(self) -> str:
        return "<default>"


_default = Default.token


def fuse(
    dsk,
    keys=None,
    dependencies=None,
    ave_width=None,
    max_width=None,
    max_height=None,
    max_depth_new_edges=None,
    rename_keys=True,
    fuse_subgraphs=False,
):
    """Fuse tasks that form reductions; more advanced than ``fuse_linear``

    This trades parallelism opportunities for faster scheduling by making tasks
    less granular.  It can replace ``fuse_linear`` in optimization passes.

    This optimization applies to all reductions--tasks that have at most one
    dependent--so it may be viewed as fusing "multiple input, single output"
    groups of tasks into a single task.  There are many parameters to fine
    tune the behavior, which are described below.  ``ave_width`` is the
    natural parameter with which to compare parallelism to granularity, so
    it should always be specified.  Reasonable values for other parameters
    will be determined using ``ave_width`` if necessary.

    Parameters
    ----------
    dsk: dict
        dask graph
    keys: list or set, optional
        Keys that must remain in the returned dask graph
    dependencies: dict, optional
        {key: [list-of-keys]}.  Must be a list to provide count of each key
        This optional input often comes from ``cull``
    ave_width: float (default 1)
        Upper limit for ``width = num_nodes / height``, a good measure of
        parallelizability.
    max_width: int (default infinite)
        Don't fuse if total width is greater than this. Set to ``None``
        to dynamically adjust to  ``1.5 + ave_width * log(ave_width + 1)``
    max_height: int or None (default None)
        Don't fuse more than this many levels. Set to None to dynamically
        adjust to ``1.5 + ave_width * log(ave_width + 1)``.
    max_depth_new_edges: int or None (default None)
        Don't fuse if new dependencies are added after this many levels.
        Set to None to dynamically adjust to ``ave_width * 1.5``
    rename_keys: bool or func, optional (default True)
        Whether to rename the fused keys with ``default_fused_keys_renamer``
        or not.  Renaming fused keys can keep the graph more understandable
        and comprehensive, but it comes at the cost of additional processing.
        If False, then the top-most key will be used.  For advanced usage, a
        function to create the new name is also accepted.
    fuse_subgraphs : bool, optional (default False)
        Whether to fuse multiple tasks into ``SubgraphCallable`` objects.
        Set to None to let the default optimizer of individual dask collections decide.
        If no collection-specific default exists, defaults to False.

    Returns
    -------
    dsk
        output graph with keys fused
    dependencies
        dict mapping dependencies after fusion.  Useful side effect to accelerate other
        downstream optimizations.
    """

    if keys is not None and not isinstance(keys, set):
        if not isinstance(keys, list):
            keys = [keys]
        keys = set(flatten(keys))

    if ave_width is None:
        ave_width = 1
    if max_height is None:
        max_height = 1.5 + (ave_width * math.log(ave_width + 1))
    if max_depth_new_edges is None:
        max_depth_new_edges = ave_width * 1.5
    if max_width is None:
        max_width = 1.5 + ave_width * math.log(ave_width + 1)

    if not ave_width or not max_height:
        return dsk, dependencies

    if rename_keys is True:
        key_renamer = default_fused_keys_renamer
    elif rename_keys is False:
        key_renamer = None
    elif not callable(rename_keys):
        raise TypeError("rename_keys must be a boolean or callable")
    else:
        key_renamer = rename_keys
    rename_keys = key_renamer is not None

    deps = {k: get_dependencies(dsk, k, as_list=True) for k in dsk} if dependencies is None else dict(dependencies)

    rdeps = {}
    for k, vals in deps.items():
        for v in vals:
            if v not in rdeps:
                rdeps[v] = [k]
            else:
                rdeps[v].append(k)
        deps[k] = set(vals)

    reducible = {k for k, vals in rdeps.items() if len(vals) == 1}
    if keys:
        reducible -= keys

    for k, v in dsk.items():
        if type(v) is not tuple and not isinstance(v, (numbers.Number, str)):
            reducible.discard(k)

    if not reducible and (not fuse_subgraphs or all(len(set(v)) != 1 for v in rdeps.values())):
        # Quick return if there's nothing to do. Only progress if there's tasks
        # fusible by the main `fuse`, or by `fuse_subgraphs` if enabled.
        return dsk, deps

    rv = dsk.copy()
    fused_trees = {}
    # These are the stacks we use to store data as we traverse the graph
    info_stack = []
    children_stack = []
    # For speed
    deps_pop = deps.pop
    reducible_add = reducible.add
    reducible_pop = reducible.pop
    reducible_remove = reducible.remove
    fused_trees_pop = fused_trees.pop
    info_stack_append = info_stack.append
    info_stack_pop = info_stack.pop
    children_stack_append = children_stack.append
    children_stack_extend = children_stack.extend
    children_stack_pop = children_stack.pop
    while reducible:
        parent = reducible_pop()
        reducible_add(parent)
        while parent in reducible:
            # Go to the top
            parent = rdeps[parent][0]
        children_stack_append(parent)
        children_stack_extend(reducible & deps[parent])
        while True:
            child = children_stack[-1]
            if child != parent:
                children = reducible & deps[child]
                while children:
                    # Depth-first search
                    children_stack_extend(children)
                    parent = child
                    child = children_stack[-1]
                    children = reducible & deps[child]
                children_stack_pop()
                # This is a leaf node in the reduction region
                # key, task, fused_keys, height, width, number of nodes, fudge, set of edges
                info_stack_append(
                    (
                        child,
                        rv[child],
                        [child] if rename_keys else None,
                        1,
                        1,
                        1,
                        0,
                        deps[child] - reducible,
                    )
                )
            else:
                children_stack_pop()
                # Calculate metrics and fuse as appropriate
                deps_parent = deps[parent]
                edges = deps_parent - reducible
                children = deps_parent - edges
                num_children = len(children)

                if num_children == 1:
                    (
                        child_key,
                        child_task,
                        child_keys,
                        height,
                        width,
                        num_nodes,
                        fudge,
                        children_edges,
                    ) = info_stack_pop()
                    num_children_edges = len(children_edges)

                    if fudge > num_children_edges - 1 >= 0:
                        fudge = num_children_edges - 1
                    edges |= children_edges
                    no_new_edges = len(edges) == num_children_edges
                    if not no_new_edges:
                        fudge += 1

                    # Sanity check; don't go too deep if new levels introduce new edge dependencies
                    if (num_nodes + fudge) / height <= ave_width and (no_new_edges or height < max_depth_new_edges):
                        # Perform substitutions as we go
                        val = subs(dsk[parent], child_key, child_task)
                        deps_parent.remove(child_key)
                        deps_parent |= deps_pop(child_key)
                        del rv[child_key]
                        reducible_remove(child_key)
                        if rename_keys:
                            child_keys.append(parent)
                            fused_trees[parent] = child_keys
                            fused_trees_pop(child_key, None)

                        if children_stack:
                            if no_new_edges:
                                # Linear fuse
                                info_stack_append(
                                    (
                                        parent,
                                        val,
                                        child_keys,
                                        height,
                                        width,
                                        num_nodes,
                                        fudge,
                                        edges,
                                    )
                                )
                            else:
                                info_stack_append(
                                    (
                                        parent,
                                        val,
                                        child_keys,
                                        height + 1,
                                        width,
                                        num_nodes + 1,
                                        fudge,
                                        edges,
                                    )
                                )
                        else:
                            rv[parent] = val
                            break
                    else:
                        rv[child_key] = child_task
                        reducible_remove(child_key)
                        if children_stack:
                            # Allow the parent to be fused, but only under strict circumstances.
                            # Ensure that linear chains may still be fused.
                            if fudge > int(ave_width - 1):
                                fudge = int(ave_width - 1)
                            # This task *implicitly* depends on `edges`
                            info_stack_append(
                                (
                                    parent,
                                    rv[parent],
                                    [parent] if rename_keys else None,
                                    1,
                                    width,
                                    1,
                                    fudge,
                                    edges,
                                )
                            )
                        else:
                            break
                else:
                    child_keys = []
                    height = 1
                    width = 0
                    num_single_nodes = 0
                    num_nodes = 0
                    fudge = 0
                    children_edges = set()
                    max_num_edges = 0
                    children_info = info_stack[-num_children:]
                    del info_stack[-num_children:]
                    for (
                        cur_key,
                        cur_task,
                        cur_keys,
                        cur_height,
                        cur_width,
                        cur_num_nodes,
                        cur_fudge,
                        cur_edges,
                    ) in children_info:
                        if cur_height == 1:
                            num_single_nodes += 1
                        elif cur_height > height:
                            height = cur_height
                        width += cur_width
                        num_nodes += cur_num_nodes
                        fudge += cur_fudge
                        if len(cur_edges) > max_num_edges:
                            max_num_edges = len(cur_edges)
                        children_edges |= cur_edges
                    # Fudge factor to account for possible parallelism with the boundaries
                    num_children_edges = len(children_edges)
                    fudge += min(num_children - 1, max(0, num_children_edges - max_num_edges))

                    if fudge > num_children_edges - 1 >= 0:
                        fudge = num_children_edges - 1
                    edges |= children_edges
                    no_new_edges = len(edges) == num_children_edges
                    if not no_new_edges:
                        fudge += 1
                    # Sanity check; don't go too deep if new levels introduce new edge dependencies
                    is_width = num_single_nodes <= ave_width and width <= max_width
                    is_height = height <= max_height and (no_new_edges or height < max_depth_new_edges)
                    if (num_nodes + fudge) / height <= ave_width and is_width and is_height:
                        # Perform substitutions as we go
                        val = dsk[parent]
                        children_deps = set()
                        for child_info in children_info:
                            cur_child = child_info[0]
                            val = subs(val, cur_child, child_info[1])
                            del rv[cur_child]
                            children_deps |= deps_pop(cur_child)
                            reducible_remove(cur_child)
                            if rename_keys:
                                fused_trees_pop(cur_child, None)
                                child_keys.extend(child_info[2])
                        deps_parent -= children
                        deps_parent |= children_deps

                        if rename_keys:
                            child_keys.append(parent)
                            fused_trees[parent] = child_keys

                        if children_stack:
                            info_stack_append(
                                (
                                    parent,
                                    val,
                                    child_keys,
                                    height + 1,
                                    width,
                                    num_nodes + 1,
                                    fudge,
                                    edges,
                                )
                            )
                        else:
                            rv[parent] = val
                            break
                    else:
                        for child_info in children_info:
                            rv[child_info[0]] = child_info[1]
                            reducible_remove(child_info[0])
                        if children_stack:
                            # Allow the parent to be fused, but only under strict circumstances.
                            # Ensure that linear chains may still be fused.
                            if width > max_width:
                                width = max_width
                            if fudge > int(ave_width - 1):
                                fudge = int(ave_width - 1)
                            # key, task, height, width, number of nodes, fudge, set of edges
                            # This task *implicitly* depends on `edges`
                            info_stack_append(
                                (
                                    parent,
                                    rv[parent],
                                    [parent] if rename_keys else None,
                                    1,
                                    width,
                                    1,
                                    fudge,
                                    edges,
                                )
                            )
                        else:
                            break
                # Traverse upwards
                parent = rdeps[parent][0]

    if fuse_subgraphs:
        _inplace_fuse_subgraphs(rv, keys, deps, fused_trees, rename_keys)

    if key_renamer:
        for root_key, fused_keys in fused_trees.items():
            alias = key_renamer(fused_keys)
            if alias is not None and alias not in rv:
                rv[alias] = rv[root_key]
                rv[root_key] = alias
                deps[alias] = deps[root_key]
                deps[root_key] = {alias}

    return rv, deps


def _inplace_fuse_subgraphs(dsk, keys, dependencies, fused_trees, rename_keys):
    """Subroutine of fuse.Mutates dsk, depenencies, and fused_trees inplace."""
    # locate all members of linear chains
    child2parent = {}
    unfusible = set()
    for parent in dsk:
        deps = dependencies[parent]
        has_many_children = len(deps) > 1
        for child in deps:
            if keys is not None and child in keys:
                unfusible.add(child)
            elif child in child2parent:
                del child2parent[child]
                unfusible.add(child)
            elif has_many_children:
                unfusible.add(child)
            elif child not in unfusible:
                child2parent[child] = parent

    # construct the chains from ancestor to descendant
    chains = []
    parent2child = {v: k for k, v in child2parent.items()}
    while child2parent:
        child, parent = child2parent.popitem()
        chain = [child, parent]
        while parent in child2parent:
            parent = child2parent.pop(parent)
            del parent2child[parent]
            chain.append(parent)
        chain.reverse()
        while child in parent2child:
            child = parent2child.pop(child)
            del child2parent[child]
            chain.append(child)
        # Skip chains with < 2 executable tasks
        ntasks = 0
        for key in chain:
            ntasks += istask(dsk[key])
            if ntasks > 1:
                chains.append(chain)
                break

    # Mutate dsk fusing chains into subgraphs
    for chain in chains:
        subgraph = {k: dsk[k] for k in chain}
        outkey = chain[0]

        # Update dependencies and graph
        inkeys_set = dependencies[outkey] = dependencies[chain[-1]]
        for k in chain[1:]:
            del dependencies[k]
            del dsk[k]

        # Create new task
        inkeys = tuple(inkeys_set)
        dsk[outkey] = (SubgraphCallable(subgraph, outkey, inkeys),) + inkeys

        # Mutate `fused_trees` if key renaming is needed (renaming done in fuse)
        if rename_keys:
            chain2 = []
            for k in chain:
                subchain = fused_trees.pop(k, False)
                if subchain:
                    chain2.extend(subchain)
                else:
                    chain2.append(k)
            fused_trees[outkey] = chain2


class SubgraphCallable:
    """Create a callable object from a dask graph.

    Parameters
    ----------
    dsk : dict
        A dask graph
    outkey : hashable
        The output key from the graph
    inkeys : list
        A list of keys to be used as arguments to the callable.
    name : str, optional
        The name to use for the function.

    """

    __slots__ = ("dsk", "outkey", "inkeys", "name")

    def __init__(self, dsk, outkey, inkeys, name="subgraph_callable"):
        self.dsk = dsk
        self.outkey = outkey
        self.inkeys = inkeys
        self.name = name

    def __repr__(self):
        return self.name

    def __eq__(self, other):
        is_key = self.outkey == other.outkey and set(self.inkeys) == set(other.inkeys)
        return type(self) is type(other) and self.name == other.name and is_key

    def __ne__(self, other):
        return not self.__eq__(other)

    def __call__(self, *args):
        if not len(args) == len(self.inkeys):
            raise ValueError("Expected %d args, got %d" % (len(self.inkeys), len(args)))
        return get(self.dsk, self.outkey, dict(zip(self.inkeys, args)))

    def __reduce__(self):
        return SubgraphCallable, (self.dsk, self.outkey, self.inkeys, self.name)

    def __hash__(self):
        return hash((self.outkey, tuple(self.inkeys), self.name))

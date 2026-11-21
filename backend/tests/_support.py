"""Turn plain test functions into unittest cases so both unittest and pytest run them."""

import unittest


def collect(namespace, prefix='test_'):
    """Publish each ``test_*`` function in ``namespace`` as a TestCase method."""
    tests = {name: value for name, value in namespace.items()
             if name.startswith(prefix) and callable(value)}
    case = type('FunctionTests', (unittest.TestCase,), {
        name: (lambda fn: lambda self: fn())(fn) for name, fn in tests.items()
    })
    namespace[case.__name__] = case
    return case

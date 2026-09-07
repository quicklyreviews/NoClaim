"""Offline harness for the *direct* tests only.

This installs a mock `genlayer` module into sys.modules, so it must stay scoped
to this directory — `tests/integration/` needs the real SDK and a real network,
and a mock leaking into it would make those tests pass against nothing.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import dataclasses
from types import ModuleType

# Patch standard dataclass to allow 'allow_storage' argument in local pytest, the
# same shim used by Genlayer agent contract/tests/conftest.py for its contracts.
original_dataclass = dataclasses.dataclass


def custom_dataclass(*args, allow_storage=False, **kwargs):
    if len(args) == 1 and callable(args[0]):
        return original_dataclass(args[0])

    def decorator(cls):
        return original_dataclass(cls, **kwargs)
    return decorator


dataclasses.dataclass = custom_dataclass

# Create mock genlayer module for local unit testing — no GenVM, no network, no LLM.
# Direct tests mock every gl.nondet.* call explicitly per-test; these are just the
# default no-op/empty behaviors so contract __init__ and simple paths don't crash
# before a test installs its own monkeypatch.
genlayer_module = ModuleType("genlayer")


class MockAddress(str):
    pass


class MockU32(int):
    pass


class MockU64(int):
    pass


class MockU128(int):
    pass


class MockU256(int):
    pass


class MockDynArray(list):
    pass


class MockTreeMap(dict):
    pass


class MockContract:
    pass


class MockMessage:
    sender_address = "0x0000000000000000000000000000000000000000"
    value = 0


class MockUserError(Exception):
    pass


class MockReturn:
    def __init__(self, calldata=None):
        self.calldata = calldata


class MockVM:
    UserError = MockUserError
    Return = MockReturn

    @staticmethod
    def run_nondet_unsafe(leader_fn, validator_fn):
        res = leader_fn()
        ok = validator_fn(MockReturn(res))
        assert ok, "validator_fn rejected leader_fn's own result — equivalence bug"
        return res


class MockWebResponse:
    def __init__(self, body=""):
        self.body = body


class MockWeb:
    @staticmethod
    def get(url):
        return MockWebResponse("{}")


class MockNondet:
    web = MockWeb()

    @staticmethod
    def get_webpage(url):
        return "{}"

    @staticmethod
    def exec_prompt(prompt, response_format="json"):
        return {}


class MockWrite:
    """`gl.public.write` is used both bare (`@gl.public.write`) and with an
    attribute (`@gl.public.write.payable`), so it has to be callable itself and
    carry a `.payable` attribute that is also a decorator."""

    def __call__(self, fn):
        return fn

    @staticmethod
    def payable(fn):
        return fn


class MockPublic:
    write = MockWrite()

    @staticmethod
    def view(fn):
        return fn


class _EvmProxy:
    """Stand-in for a `@gl.evm.contract_interface`-decorated class: accepts the
    address the real interface would take, and any method call on it (write or
    view) is a no-op — external transfers are mocked out in direct tests."""

    def __init__(self, address):
        self.address = address

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            return None
        return _call


class MockEvm:
    @staticmethod
    def contract_interface(cls):
        return _EvmProxy


class MockGL:
    Contract = MockContract
    message = MockMessage()
    vm = MockVM()
    nondet = MockNondet()
    public = MockPublic()
    evm = MockEvm()


def allow_storage(cls):
    return cls


genlayer_module.gl = MockGL()
genlayer_module.Address = MockAddress
genlayer_module.u32 = MockU32
genlayer_module.u64 = MockU64
genlayer_module.u128 = MockU128
genlayer_module.u256 = MockU256
genlayer_module.DynArray = MockDynArray
genlayer_module.TreeMap = MockTreeMap
genlayer_module.allow_storage = allow_storage

sys.modules["genlayer"] = genlayer_module
